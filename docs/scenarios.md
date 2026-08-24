# Common Scenarios

## New player joins mid-campaign

They'll appear as `Unknown Speaker N` in the output. Fix and enroll them:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper enroll "Frank" --audio session05.mp3 --segment "5:00-6:30"
```

Future sessions will recognize Frank automatically.

---

## Speaker sounds different (sick, new mic, remote)

Re-enroll with recent audio to blend it into their profile:

```bash
wisper enroll "Alice" --audio session08.mp3 --update
```

The `--update` flag averages the new sample with the existing profile using an exponential moving average, making recognition more robust over time.

---

## Player absent from a session

No problem — their profile is simply ignored for that file. Unused profiles never cause errors.

---

## Wrong automatic match

```bash
wisper fix session03.md --speaker "Alice" --name "Diana"
```

---

## Improve transcription accuracy for character names and locations

Pass a custom word list to boost recognition of proper nouns Whisper doesn't know:

```bash
wisper transcribe session01.mp3 --vocab-file characters.txt
```

`characters.txt` — one word per line, `#` comments ignored:
```
# Glass Cannon characters
Kyra
Golarion
Zeldris
Korvosa
```

To apply hotwords to every future transcription automatically, save them to config:

```bash
wisper config set hotwords "Kyra, Golarion, Zeldris, Korvosa"
```

The `--vocab-file` flag takes precedence over the stored config when both are present.

---

## Known Limitations (v1)

- **One active recording at a time.** `BotManager` (Discord) and `LocalCaptureManager` (local mic + system audio) share a single-session invariant across *both* of them — starting a second recording (of either kind) while one is active returns an error.
- **No multi-guild / multi-channel.** The bot connects to one voice channel in one guild per session.
- **DAVE E2EE voice receive depends on JDAVE (Java).** Discord's DAVE protocol encrypts per-user voice — only JDA+JDAVE has confirmed working decrypt as of 2026-05. When [Pycord PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) ships DAVE support, the Java sidecar can be replaced with a ~100-line Python implementation. The Unix-socket wire protocol is the stable interface.
- **Discord recordings are still batch-only.** Live transcription is implemented for local mic + system-audio recording (see [Local Recording](web-ui.md#local-recording-mic--system-audio)); Discord sessions are still batch-transcribed after the session stops. The five recording-layer file-format invariants were kept deliberately source-agnostic, so extending live transcription to Discord recordings later wouldn't require rewriting the recording layer.
- **Live transcription is a draft, not the real transcript.** No speaker diarization in the live path (too heavy to run per few-second chunk) — lines are labeled "You"/"Other" by comparing mic vs. system audio energy, not by voice identity. On CPU-only machines, live transcription competes with capture for CPU time; use `base`/`small` rather than a large model to keep the preview responsive. GPU machines can use any model size. The authoritative, fully-diarized transcript is always the post-session pass via **Transcribe**.
- **No auth on web routes.** `wisper server` binds `127.0.0.1` by default; exposing it with `--host 0.0.0.0` extends full read-write control (including recording start/stop) to anyone who can reach the port — see the [trust model](web-ui.md#trust-model). Project-wide auth is tracked in the backlog.
