# wisper-transcribe — Open Items

---

## Deferred parity gaps

### D5 — Refine/summarize CLI vs web asymmetry
CLI runs these synchronously with `--dry-run` preview. Web runs them as async JobQueue jobs with no dry-run. Both work; the asymmetry reflects the surface (terminal vs. browser), not a missing feature.

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
