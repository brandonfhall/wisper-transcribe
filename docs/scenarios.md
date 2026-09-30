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

## Same unknown guest across several sessions

Put the sessions in a campaign and run **Re-match speakers** on the Campaign page (or `wisper campaigns relabel <slug>`). A voice that isn't enrolled but shows up in two or more sessions gets one shared name, `Recurring Speaker N`, in all of them. Name them in any one session's speaker wizard; the other sessions are renamed automatically.

---

## Re-enrolling after the speaker-model upgrade

Voice profiles enrolled before the switch to `speaker-diarization-community-1` came from a different embedding model and can't be compared with new sessions. They are skipped during matching (the job log names them), and the **Speakers** page marks them **NEEDS RE-ENROLL**.

Re-enroll each one by naming them again, which replaces the old voice data:

- **Web:** open a transcript they appear in, click **Name speakers**, and pick their existing name.
- **CLI:** `wisper enroll "Alice" --audio session08.mp3 --update`

Enrolling from a long session gives a better profile than from a short clip.

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

## Checking word alignment on your own audio

Forced alignment re-times each word before it's given to a speaker. To see what it changes on one of your recordings, run the measurement script on a short excerpt with crosstalk (output goes to `alignment-eval/`, which is gitignored):

```bash
python scripts/alignment_eval.py run session.mp3 --start 1800 --duration 180 --out alignment-eval/s1
python scripts/alignment_eval.py audit alignment-eval/s1   # re-listens to words moved >1 s
python scripts/alignment_eval.py sheet alignment-eval/s1   # blind labelling sheet
# listen to alignment-eval/s1/clip.wav and fill correct_speaker in sheet.csv
python scripts/alignment_eval.py score alignment-eval/*/
```

`run` prints an automatic proxy for each variant (Whisper vs aligned timing, smoothing on/off, regular vs exclusive diarization). `score` reports how often each variant gave the words around speaker changes to the right person, according to your labels.

## Known Limitations

- **One recording at a time.** Starting a Discord or local recording while either kind is active is rejected.
- **One voice channel per Discord session.** No multi-guild or multi-channel recording.
- **Discord recording needs Java.** Discord encrypts voice end-to-end (DAVE), and only the Java JDA + JDAVE sidecar can decrypt it today. It will move to Python once a stable Python library supports DAVE receive.
- **Live transcription is local-only.** Discord sessions are transcribed after they stop.
- **The live preview is a draft.** Lines are labelled "You" or "Other" by comparing mic and system-audio volume, not by voice. The diarized transcript from **Transcribe** is the real one. On CPU-only machines, use `base` or `small` so the preview keeps up.
- **Live preview waits for other jobs.** Jobs run one at a time, so a job already running when a local session starts delays the preview until it finishes.
- **Cancelling is best-effort.** A cancelled transcription stops at its next progress update; the GPU may finish its current batch first.
- **No web authentication.** `wisper server` binds `127.0.0.1` by default. With `--host 0.0.0.0`, anyone who can reach the port has full control, including recording — see the [trust model](web-ui.md#trust-model).
