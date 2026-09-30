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

The sheet lists rows marked `discriminating` first: those are the words where the variants disagree, and they decide the result. The proxy is only a sanity check; trust the labels. Live-table recordings are the best test audio, since edited podcasts have little timing drift to fix.

## A transcript was renamed or deleted outside wisper

wisper keeps a record of every transcript in the transcripts folder. If a file vanishes (deleted or renamed in Finder or Obsidian, a sync that hasn't finished, a drive that isn't connected), its campaign entry stays in place and shows **MISSING** instead of being dropped.

- **It comes back** (sync finishes, drive reconnected): nothing to do; the flag clears the next time you open the Campaign or Transcripts page.
- **It was renamed:** on the Campaign page, choose the new name in the **Relink** dropdown next to the missing entry. The session keeps its place in the episode order, its journal entry, and its speaker names. Renaming only the capitalization (e.g. `session 1` → `Session 1`) is picked up automatically on macOS and Windows.
- **It's really gone:** remove it from the campaign with ✕. If it had been folded into the journal, the journal is marked as needing a rebuild.

`wisper transcripts list` shows missing entries too.

## A recording stopped unexpectedly

If wisper crashes or the machine loses power mid-session, the recording shows **FAILED** after the restart, but everything captured up to the last minute is still on disk. Open the recording and click **Recover recording** (or run `wisper record recover <recording_id>` while `wisper server` runs). The saved pieces are joined into one file, the recording becomes **COMPLETED**, and **Transcribe** works as usual. Expect up to the final minute to be missing.

## Starting the campaign journal over

- **Rebuild journal** (web) or `wisper campaigns journal <slug> --rebuild` re-folds every session's existing summary: one LLM call per session, and your edits to summaries are kept.
- **Rebuild from transcripts** / `--rebuild --resummarize` re-summarizes every session first (two calls per session). Use it after switching LLM model or when the summaries are poor.
- Deleting `journal.md` from `campaigns/<slug>/` also starts over: every summarized session becomes pending again. Editing `journal.md` by hand is fine; later folds build on your version.

## Known Limitations

- **One recording at a time.** Starting a Discord or local recording while either kind is active is rejected.
- **One voice channel per Discord session.** No multi-guild or multi-channel recording.
- **Discord recording needs Java.** Discord encrypts voice end-to-end (DAVE), and only the Java JDA + JDAVE sidecar can decrypt it today. It will move to Python once a stable Python library supports DAVE receive.
- **Live transcription is local-only.** Discord sessions are transcribed after they stop.
- **The live preview is a draft.** Lines are labelled "You" or "Other" by comparing mic and system-audio volume, not by voice. The diarized transcript from **Transcribe** is the real one. On CPU-only machines, use `base` or `small` so the preview keeps up.
- **Live preview waits for other jobs.** Jobs run one at a time, so a job already running when a local session starts delays the preview until it finishes.
- **Word alignment needs a GPU by default.** On CPU-only machines `forced_alignment = auto` leaves it off (it adds ~10–20 min per 2.5 h session); set it to `true` to use it anyway. It supports 11 languages (English, Chinese, Cantonese, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish); others keep Whisper's word timing. It can't split words when two people talk at once.
- **Cancelling is best-effort.** A cancelled transcription stops at its next progress update; the GPU may finish its current batch first.
- **No web authentication.** `wisper server` binds `127.0.0.1` by default. With `--host 0.0.0.0`, anyone who can reach the port has full control, including recording — see the [trust model](web-ui.md#trust-model).
