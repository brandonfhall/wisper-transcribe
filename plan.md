# `feat/discord-recording-bot` — Branch Review Plan

> Replaces the previous "Discord Recording Bot v1 — Completion Summary" content (preserved in git history). This is the active polish plan generated from a top-to-bottom branch review on 2026-05-11.

---

## Execution context (read before starting)

You (Sonnet) are picking up this plan from Opus. The repo is `wisper-transcribe`, branch `feat/discord-recording-bot`. Before any edits, read `CLAUDE.md` at the repo root — it's authoritative and overrides anything below. Highlights:

**Definition of Done** (run for every commit, in order):
1. `.venv/bin/pytest tests/ -v` → green
2. Update `architecture.md` for any module/pipeline/config-key change. Update `README.md` for any user-facing change.
3. If any template (`.html`) or `static/input.css` changed: rebuild Tailwind with
   `.venv/bin/python -m pytailwindcss -i src/wisper_transcribe/static/input.css -o src/wisper_transcribe/static/tailwind.min.css --minify`
   and commit `tailwind.min.css` alongside the template change.
4. Single `git commit` per phase. Push after each commit (`git push -u origin feat/discord-recording-bot`). **Pause for user review between phases.** Commits are pre-authorized — no separate permission needed.

**The CodeQL path-traversal pattern is load-bearing.** Section C1 consolidates four near-identical validators. Before/after the change, `tests/test_path_traversal.py` must stay green AND CodeQL must remain clean on the PR. The pattern that CodeQL recognises is exactly:
```python
def validate_path_component(value: str, guard_name: str) -> str | None:
    if not value or "\x00" in value:
        return None
    safe = os.path.basename(value)
    if safe != value or safe in {".", ".."}:
        return None
    if not re.match(r"^[\w\-]+$", safe):
        return None
    _guard_base = os.path.abspath(guard_name)
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None
    return os.path.basename(_guard_path)
```
**Do not "simplify" this.** `re.match()` results are still tainted to CodeQL — the `os.path.abspath` + `startswith` round-trip is what breaks the taint chain. `Path.resolve()` is NOT a substitute. See CLAUDE.md "Web Route Security Standards" for the full rule.

Keep the original module-level names (`_validate_job_id`, `_validate_recording_id`, `_validate_campaign_slug`, `_validate_profile_key`) as thin wrappers over the consolidated function — CodeQL sees each call site as sanitised, and you avoid touching ~20 call sites in route handlers. Example wrapper:
```python
def _validate_recording_id(recording_id: str) -> str | None:
    return validate_path_component(recording_id, "_recordings_guard")
```

**Test workflow:**
- Quick run: `.venv/bin/pytest tests/ -v`
- Single file: `.venv/bin/pytest tests/test_discord_bot.py -v`
- Single test: `.venv/bin/pytest tests/test_path_traversal.py::test_recording_id_rejects_null_byte -v`
- Coverage parity with CI: `.venv/bin/pytest tests/ -v --cov --cov-report=term-missing`

**Web UI verification:** Start dev server with `wisper server --reload` (http://localhost:8080). For any UI change (D-series, E-series) actually click through the affected page in a browser — type-checks and tests don't verify rendering. CLAUDE.md is explicit about this.

**Existing patterns to mirror (don't reinvent):**
- Route handler validation: [src/wisper_transcribe/web/routes/transcribe.py:23-48](src/wisper_transcribe/web/routes/transcribe.py#L23-L48) (the current `_validate_job_id`). After C1 the body is replaced but the signature stays.
- Form route signatures: [src/wisper_transcribe/web/routes/transcribe.py:77-94](src/wisper_transcribe/web/routes/transcribe.py#L77-L94) shows the `Annotated[X, Form()]` style used throughout.
- Redirect with error: [src/wisper_transcribe/web/routes/record.py:340](src/wisper_transcribe/web/routes/record.py#L340) shows the `?error=<code>` convention. **Never put `str(exc)` in a redirect URL** — generic error codes only (CLAUDE.md rule).
- Per-recording mutex: [src/wisper_transcribe/recording_manager.py:29-38](src/wisper_transcribe/recording_manager.py#L29-L38) is the model for any new lock you might need.
- Logging idiom: `log = logging.getLogger(__name__)` at module top, used in [src/wisper_transcribe/web/discord_bot.py:21](src/wisper_transcribe/web/discord_bot.py#L21).

**Branch / git:**
- Stay on `feat/discord-recording-bot`. Don't create a new branch unless the user asks.
- Never push to `main`. All work merges via PR.
- Commits Co-Authored-By line goes at the end (CLAUDE.md commit format).

**Gotchas (already documented but easy to miss):**
- `_model` and `_pipeline` are module-level globals in `transcriber.py` / `diarizer.py` — not thread-safe. JobQueue is intentionally single-worker. Don't add parallel job processing.
- `static/htmx.min.js` is a placeholder in dev; the real file is fetched in Docker build. If you touch htmx behavior, fetch the real file with: `curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" -o src/wisper_transcribe/static/htmx.min.js`
- Tailwind auto-rebuilds on `wisper server` startup via mtime check in `app.py`, but you must rebuild + commit `tailwind.min.css` for the PR itself.
- All paths use `pathlib.Path`, not strings (cross-platform).
- HF token resolution: env `HUGGINGFACE_TOKEN` or `HF_TOKEN` (both accepted), or `config.toml`. Both env names get propagated to `os.environ` once resolved.

**If a CodeQL run flags a new finding** after C1: do not silence it with `# lgtm` or `# nosec`. The taint pattern in this codebase is "show CodeQL an `os.path.abspath`/`startswith` chain". Match the existing shape.

---

## Context

The branch is a substantial feature addition — a Discord voice-channel recording bot via a Java/JDA sidecar plus Python integration layer, recording manager, segmented Ogg/Opus writer, real-time PCM mixer, web control panel, CLI surface, and JobQueue hand-off. The branch is functionally complete and CI-green.

A top-to-bottom review of code, docs, tests, security posture, and feature parity surfaces:
- a small set of real-but-low-blast-radius security / correctness papercuts,
- meaningful code-duplication opportunities (4–5 near-identical CodeQL path-traversal validators),
- a handful of CLI ↔ web parity gaps,
- some quality-of-life feature gaps (search, segment browser, bot-status surface),
- and a doc hygiene fix (test counts are listed in docs and drift constantly).

Everything below is grouped by priority. **None of these are merge blockers.** The branch is in good shape; this is polish.

---

## Section A — Documentation fixes (trivial, do first)

- **A1.** Remove test counts from the docs entirely — they drift out of date with every commit. Touch:
  - [architecture.md:369](architecture.md#L369) ("Test count: 662 (all mocked, all passing)") — delete the line.
  - `CLAUDE.md` "Documentation Rules" table: drop "or test count change" from the `architecture.md` trigger list so future contributors aren't told to keep it in sync.
  - Grep for any other stray mentions: `rg -n "\b[5-9]\d\d tests\b|test_?count" --hidden`. Remove any.

---

## Section B — Security & correctness fixes (real, but not merge-blocking)

### B1. Bogus fallback Discord token in `_session_loop()`

[src/wisper_transcribe/web/discord_bot.py:323-328](src/wisper_transcribe/web/discord_bot.py#L323-L328):
```python
token = os.environ.get("DISCORD_BOT_TOKEN", "")
if not token:
    token = load_config().get("discord_bot_token", "")
if not token:
    token = recording.voice_channel_id  # fallback for test introspection
```
The third fallback ships a **voice-channel snowflake** to the JDA sidecar as if it were a bot token. Discord will reject it, so functionally this only matters for tests, but:
- it leaks the voice channel ID into the spawn-args of the JDA subprocess (visible via `ps`),
- it produces a noisy/confusing failure mode in production if config is misconfigured,
- it conflates "no token" (configuration error) with "test injection".

**Fix:** Replace with an explicit branch — when token is empty and the source factory is not the production `_unix_socket_source` (i.e. tests injected a fake source), pass a sentinel like `"__test_token__"`. Otherwise, abort the session with status `failed` and a clear log/error.

### B2. Silent `except Exception` swallows in `routes/record.py`

Three sites in [src/wisper_transcribe/web/routes/record.py](src/wisper_transcribe/web/routes/record.py) catch everything and either redirect or `pass` with no log:
- L354 `enroll_speaker_from_audio_dir(...)` → redirect with `?error=enroll_failed`
- L375 campaign auto-binding after enrollment → `pass`
- L426 `move_transcript_to_campaign(...)` inside the JobQueue completion callback → `pass`

These are appropriate at the user-facing layer (no info disclosure in error messages — CLAUDE.md rule) but they're invisible operationally.

**Fix:** Add a `logging.getLogger(__name__)` and `log.warning("...", exc_info=True)` before the redirect/pass at each site. Keep the redirect/pass behaviour. Same pattern already used elsewhere in the codebase.

Also check [src/wisper_transcribe/web/routes/config.py:79,102](src/wisper_transcribe/web/routes/config.py#L79) (HTTP probe → `{"running": False}` on any exception) — same fix, log before swallowing.

### B3. Authorization gap on `POST /api/record/{start,stop}`

The recording control routes are unauthenticated. Anyone with network access to the web server can start/stop recordings. Mitigated in practice by:
- single-recording lock (`BotManager` rejects concurrent starts),
- Discord-side auth (bot must have channel join permissions),
- the documented v1 deployment model is localhost-only.

**Fix:** Add an explicit note at the top of `routes/record.py` and in `architecture.md`'s "Known Constraints" table that recording control endpoints assume trusted local access. v2 auth is already deferred.

### B4. Document that recording sessions are intentionally unbounded

`_session_loop()` runs until externally stopped — this is by design (D&D/podcast sessions routinely run multiple hours). No hard cap should be enforced.

**Fix:** Add a one-line note to `architecture.md` "Known Constraints" table making this explicit, so future contributors don't see "no timeout" and assume it's a bug. Mention the operator is responsible for stopping sessions and that disk usage scales linearly with wall-clock time (~per-segment manifest growth is bounded by `segment_length=60s` so ~60 entries/hour/user).

No code change.

### B5. `_validate_job_id` is less defensive than its siblings

[src/wisper_transcribe/web/routes/transcribe.py:23-48](src/wisper_transcribe/web/routes/transcribe.py#L23-L48) skips the explicit null-byte / `.` / `..` / `os.path.basename` first-pass that `_validate_recording_id`, `_validate_campaign_slug`, and `_validate_profile_key` all do. The regex `^[\w\-]+$` does cover those cases in practice, but the sibling validators do belt-and-suspenders.

**Fix:** Bring the four validators to the same shape (see C1 — they should all become one function anyway).

---

## Section C — Code-quality refactors (consolidation)

### C1. Consolidate the four near-identical path-component validators

All four follow the same CodeQL-recognised 4-step pattern (null-byte → basename → regex → abspath/startswith round-trip):

| File | Function |
|------|----------|
| [src/wisper_transcribe/web/routes/transcribe.py:23](src/wisper_transcribe/web/routes/transcribe.py#L23) | `_validate_job_id` |
| [src/wisper_transcribe/recording_manager.py:66](src/wisper_transcribe/recording_manager.py#L66) | `_validate_recording_id` |
| [src/wisper_transcribe/campaign_manager.py:45](src/wisper_transcribe/campaign_manager.py#L45) | `_validate_campaign_slug` |
| [src/wisper_transcribe/campaign_manager.py:73](src/wisper_transcribe/campaign_manager.py#L73) | `_validate_profile_key` |

Plus inline copies in [src/wisper_transcribe/web/routes/speakers.py:38-66](src/wisper_transcribe/web/routes/speakers.py#L38-L66) and [:92-109](src/wisper_transcribe/web/routes/speakers.py#L92-L109).

**Fix:** Create [src/wisper_transcribe/path_utils.py](src/wisper_transcribe/path_utils.py) with a single `validate_path_component(value: str, guard_name: str = "_guard") -> str | None`. Keep thin module-level aliases at the original call sites so import paths don't change (and CodeQL still recognises each call as sanitised). Add a small `tests/test_path_utils.py`.

**Critical:** Verify the consolidated function still defeats CodeQL after PR. The taint-tracker has historically been finicky — keep the `os.path.abspath`/`startswith` round-trip identical to what it sees today. Run the existing `tests/test_path_traversal.py` as the regression gate.

### C2. Consolidate output-directory resolution

Two near-identical functions today:
- [src/wisper_transcribe/web/routes/transcribe.py:51](src/wisper_transcribe/web/routes/transcribe.py#L51) → `_default_output_dir()`
- [src/wisper_transcribe/web/routes/transcripts.py:26](src/wisper_transcribe/web/routes/transcripts.py#L26) → `_output_dir(request)`

The `request` param in the second is unused. Both check `./output` then fall back to `$DATA_DIR/output`.

**Fix:** Move to `wisper_transcribe/paths.py` (or `path_utils.py` from C1) as `get_output_dir()`. Replace call sites.

### C3. Extract a `safe_redirect_with_error()` helper

The `routes/record.py`, `routes/speakers.py`, `routes/transcribe.py` files each repeat the same pattern ~20 times:
```python
safe_id = _validate_*()
if safe_id is None:
    return HTMLResponse(content="Invalid X", status_code=400)
```
and
```python
return RedirectResponse(url=f"/x/{safe_id}?error=code", status_code=303)
```

**Fix (light-touch):** Add `def invalid_input_response(field: str) -> HTMLResponse` and `def redirect_with_error(base: str, code: str) -> RedirectResponse` helpers in `web/_responses.py`. Replace the ~20 sites. No behaviour change; just shrinks routes by ~80 lines.

(Not a decorator — decorators around FastAPI route handlers get messy with `Annotated[Form()]` signatures.)

### C4. Refactor `_session_loop()` into smaller helpers

[src/wisper_transcribe/web/discord_bot.py:321-366](src/wisper_transcribe/web/discord_bot.py#L321-L366) mixes token resolution, retry bookkeeping, and frame routing.

**Fix:** Extract `_resolve_discord_token(recording) -> str | None` (also addresses B1) and `_handle_session_iteration(...)` for the retry inner loop. Goal: each function ≤ 25 lines.

Low priority — works fine today, this is purely readability.

---

## Section D — CLI ↔ Web parity gaps

The CLI is more capable than the web in several places. Each gap below has a fix decision the user should make (do / defer / no).

### D1. Web `/transcribe` form is missing CLI flags
- `--vocab-file` (custom hotwords upload) — web has no equivalent. Useful for game/D&D campaigns with proper nouns.
- `--workers N` (parallel folder processing) — web only accepts single-file uploads, so this is N/A unless we also add folder/zip upload.
- `--play-audio` — web already has this as part of the enrollment wizard; no gap, just different UX.

**Recommend:** Add a "Vocabulary boost (optional .txt)" file field to the web form. Defer `--workers` until folder upload exists. Skip `--play-audio`.

### D2. Discord recording control: CLI is richer than the web bot UI
CLI exposes `wisper record status`, `wisper record channels`, `wisper record show <id>`, `wisper record transcribe <id>`, `wisper record delete <id>`. Web has these as JSON endpoints (`/api/record/...`, `/api/recordings/...`) but several aren't surfaced in the HTML pages:
- **Re-queue transcription** for a completed recording — exists in [routes/record.py:387](src/wisper_transcribe/web/routes/record.py#L387) but unclear if the `/recordings/{id}` page has a button for it. Verify and add if missing.
- **Bot/channel discovery** — CLI `wisper record channels` lists guilds and voice channels. Web has no equivalent. Useful when configuring presets.
- **Recording status panel** — `/record` has live SSE; verify it shows segment count, current speakers, and elapsed time.

**Recommend:** Add a "Channels available" panel to `/record` (calls `/api/record/channels`). Add a "Re-queue transcription" button on `/recordings/{id}` if not already there.

### D3. Discord preset management
- CLI: `wisper config discord-presets` (full list/add/remove subcommands per docs).
- Web: presets are visible in `/config` and selectable on `/record`, but no preset CRUD UI in the web.

**Recommend:** Add a "Discord presets" section to `/config` with add/remove. Or defer — this is power-user territory.

### D4. Speaker management asymmetries
- CLI has `speakers rename`, `speakers test`, `speakers test --campaign <slug>`.
- Web has enrollment + list + remove, but no dedicated rename UI and no campaign-scoped "test" preview.

**Recommend:** Add a rename action to the speaker row in `/speakers`. Defer the test/preview UI (low value vs. complexity).

### D5. Refine / summarize behave differently
- CLI: synchronous, `--dry-run` first.
- Web: async via JobQueue, no dry-run preview.

**Recommend:** Defer. Both work; the asymmetry reflects the surface (terminal vs. browser) rather than a real gap.

---

## Section E — Suggested new features / QoL adds

These are user-experience gaps, not bugs. Listed for the user to pick from.

### E1. Search & filter
- Transcripts list: no search by title / speaker / date.
- Recordings list: filter is by campaign only.
- Speakers list: no search by name / role.

**Recommend:** Add a single client-side `<input>` filter to each list page (HTMX or vanilla JS). Cheap, big QoL win.

### E2. Recording segment browser
On `/recordings/{id}` add a table of segments with: index, duration, filesize, download link, per-user-track links. The data is already in `segment_manifest`; just needs a template.

### E3. Bulk operations
- Bulk delete on transcripts list.
- Bulk campaign-assign on transcripts list.

**Recommend:** Defer until users ask. Single-item ops are sufficient for v1.

### E4. Bot connection status surface
- `/config` and `/record` could show "Bot last seen: 2s ago" / "Bot disconnected — reconnecting (attempt 3/5)".
- Useful when troubleshooting "why is my recording not capturing audio".

The data is already in `recording.rejoin_log` and `recording.status` (incl. `"degraded"`). Just needs UI.

### E5. Speaker match confidence display
When a recording auto-resolves a Discord ID to a profile, the UI shows the matched name but no similarity score / "how sure was the bot." Useful for catching mis-bindings.

**Recommend:** Defer. Auto-resolution today is by hard Discord-ID binding (Option A), so confidence is binary — bound or unbound. Only relevant once voice-print fallback (Option C) ships in v2.

### E6. Missing-module test files
Three modules without `tests/test_<module>.py`:
- `_noise_suppress.py`
- `web/app.py`
- `web/jobs.py`

These are partially covered transitively (the app fixture exercises `app.py`, route tests exercise `jobs.py`), but a dedicated unit-test file would help when refactoring. **Recommend:** add `test_web_jobs.py` covering JobQueue submit/cancel/lifecycle and `on_complete` callback. Skip `_noise_suppress.py` (pure logging filter) and `web/app.py` (mostly wiring).

---

## Critical files

- [src/wisper_transcribe/web/discord_bot.py](src/wisper_transcribe/web/discord_bot.py) (B1, C4)
- [src/wisper_transcribe/web/routes/record.py](src/wisper_transcribe/web/routes/record.py) (B2, D2)
- [src/wisper_transcribe/web/routes/transcribe.py](src/wisper_transcribe/web/routes/transcribe.py) (B5, C1, C2, D1)
- [src/wisper_transcribe/web/routes/transcripts.py](src/wisper_transcribe/web/routes/transcripts.py) (C2)
- [src/wisper_transcribe/web/routes/speakers.py](src/wisper_transcribe/web/routes/speakers.py) (B5, C1, D4)
- [src/wisper_transcribe/web/routes/config.py](src/wisper_transcribe/web/routes/config.py) (B2, D3)
- [src/wisper_transcribe/recording_manager.py](src/wisper_transcribe/recording_manager.py) (B5, C1)
- [src/wisper_transcribe/campaign_manager.py](src/wisper_transcribe/campaign_manager.py) (B5, C1)
- `src/wisper_transcribe/path_utils.py` (new — C1, C2)
- `src/wisper_transcribe/web/_responses.py` (new — C3)
- [architecture.md](architecture.md) (A1, B3, B4 — Known Constraints table)
- [README.md](README.md) (D1, D2, D3 if implemented)
- [CLAUDE.md](CLAUDE.md) (A1 — drop test-count rule)

---

## Suggested execution order

1. ~~**Phase 1 — Doc + low-risk fixes** — A1 + B1 + B2 + B3 + B4 — DONE~~

2. ~~**Phase 2 — Validator consolidation** — C1 + B5 — DONE~~

3. ~~**Phase 3 — Output-dir + response helpers** — C2 + C3 — DONE~~

4. **Phase 4 — CLI ↔ web parity** (1 commit per item):
   - ~~D1 — vocab-file upload on /transcribe — DONE~~
   - D2 — re-queue button on recording detail (in progress); channels panel deferred (needs Java sidecar protocol changes)
   - ~~D4 — speaker rename UI — already present in speakers.html, no work needed~~
   Skip D3, D5 by default.

5. **Phase 5 — QoL features (1 commit per item, optional)**
   E1 (search/filter), E2 (segment browser), E4 (bot status surface), E6 (`test_web_jobs.py`). Skip E3, E5 by default.

6. **Phase 6 — Refactor (optional)**
   C4 (`_session_loop` decomposition). Pure readability; skip if low value.

Each phase rebuilds Tailwind if templates change and updates `architecture.md` / `README.md` per CLAUDE.md Documentation Rules.

---

## Verification

For each phase:
- `.venv/bin/pytest tests/ -v` (must pass).
- For UI changes: `wisper server --reload`, click through the affected page in a browser.
- For C1: `tests/test_path_traversal.py` is the regression gate. After consolidation, push a draft PR and confirm CodeQL is clean before merging.
- For D1 (vocab-file): manual test — upload a `.txt` with `["Aragorn", "Gandalf"]` and confirm those appear in the transcript.
