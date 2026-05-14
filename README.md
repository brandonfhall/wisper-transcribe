# wisper-transcribe

Local podcast transcription with automatic speaker identification. Built for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers, but works for any multi-speaker audio.

Runs entirely offline. No cloud APIs. Outputs clean markdown files ready for NotebookLM or any text search tool.

---

## Requirements

- Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) on your PATH
- A free [HuggingFace token](https://huggingface.co/settings/tokens) (for speaker identification)
- GPU recommended but not required (CPU works, just slower)

---

## Quick Start

The fastest path: double-click to launch.

| Platform | Steps |
|----------|-------|
| **macOS** | Double-click `start.command` in Finder |
| **Windows** | Double-click `start.bat` |
| **Linux** | `bash start.sh` in a terminal |

The first run takes 5–10 minutes to download ~2 GB of ML models. Subsequent launches are instant. Your browser opens automatically to `http://localhost:8080`.

→ **[See docs/setup.md](docs/setup.md)** for Docker, developer/CLI, and manual install paths.

---

## What You Get

Each audio file produces a `.md` transcript with speaker labels and timestamps:

```markdown
**Alice** *(00:00:12)*: Welcome back everyone. Last session you had just entered the ruins of Khar'zul.

**Bob** *(00:00:18)*: Right, I want to check for traps before we go further in.
```

YAML frontmatter (title, date, speakers, duration) makes these files easy to ingest into NotebookLM or Obsidian.

---

## Documentation

| Topic | |
|-------|-|
| [Setup & installation](docs/setup.md) | All install paths, HuggingFace token, model size guide |
| [CLI reference](docs/cli-reference.md) | Every `wisper` command with full options |
| [Web UI guide](docs/web-ui.md) | Page-by-page guide, enrollment wizard, LLM post-processing |
| [Docker & Discord bot](docs/docker.md) | Container setup, volume layout, recording bot |
| [Configuration](docs/configuration.md) | Env vars, data storage, debugging flags |
| [Common scenarios](docs/scenarios.md) | How-to guides and known limitations |
| [Architecture](architecture.md) | Technical deep-dive (for contributors) |
