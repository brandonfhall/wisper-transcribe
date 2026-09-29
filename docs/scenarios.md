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

## Known Limitations

- **One recording at a time.** Starting a Discord or local recording while either kind is active is rejected.
- **One voice channel per Discord session.** No multi-guild or multi-channel recording.
- **Discord recording needs Java.** Discord encrypts voice end-to-end (DAVE), and only the Java JDA + JDAVE sidecar can decrypt it today. It will move to Python once a stable Python library supports DAVE receive.
- **Live transcription is local-only.** Discord sessions are transcribed after they stop.
- **The live preview is a draft.** Lines are labelled "You" or "Other" by comparing mic and system-audio volume, not by voice. The diarized transcript from **Transcribe** is the real one. On CPU-only machines, use `base` or `small` so the preview keeps up.
- **Live preview waits for other jobs.** Jobs run one at a time, so a job already running when a local session starts delays the preview until it finishes.
- **Cancelling is best-effort.** A cancelled transcription stops at its next progress update; the GPU may finish its current batch first.
- **No web authentication.** `wisper server` binds `127.0.0.1` by default. With `--host 0.0.0.0`, anyone who can reach the port has full control, including recording — see the [trust model](web-ui.md#trust-model).
