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

- **One active recording at a time.** `BotManager` manages a single Discord voice session — starting a second recording while one is active returns an error.
- **No multi-guild / multi-channel.** The bot connects to one voice channel in one guild per session.
- **DAVE E2EE voice receive depends on JDAVE (Java).** Discord's DAVE protocol encrypts per-user voice — only JDA+JDAVE has confirmed working decrypt as of 2026-05. When [Pycord PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) ships DAVE support, the Java sidecar can be replaced with a ~100-line Python implementation. The Unix-socket wire protocol is the stable interface.
- **Live transcription is deferred to v2.** Recordings are batch-transcribed after the session stops. Five file-format invariants are honoured so v2 can add live transcription without rewriting the recording layer.
- **No auth on web routes.** The existing "trust your LAN" posture applies to recording start/stop controls. Project-wide auth is tracked in the backlog.
