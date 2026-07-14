"""Shared logic for the speaker-enrollment wizard.

Both enrollment entry points -- the transcript-centric wizard
(``web/routes/transcripts.py``) and the legacy job-centric wizard
(``web/routes/transcribe.py``) -- need to:

1. Resolve the *current* display name for each raw pyannote label so a
   second pass through the wizard actually finds something to rename
   (F1 -- the raw label stops existing in the markdown body as soon as any
   profile exists and ``match_speakers`` writes display names into it).
2. Refuse to create voice profiles named after the raw pyannote label itself
   (``SPEAKER_03`` etc.) -- those are never real, and once enrolled they
   compete in every future ``match_speakers`` call (F2).
3. Merge into an existing profile's embedding via EMA (``update_embedding``)
   instead of overwriting it with ``enroll_speaker`` on every resubmission,
   and average embeddings when two raw labels are assigned the same display
   name in one submit (F3).

This module holds that logic once so both routes call the same code path.

**Phase 2.5 split:** what used to be a single ``apply_enrollment_submit()``
(rename + convert-to-WAV + extract embeddings, all synchronous inside the
HTTP request) is now two functions so the slow half can run in the
background ``JobQueue`` instead of blocking the browser tab for 30-120s:

- ``apply_renames()`` -- fast, synchronous. Rewrites the transcript markdown
  and returns the validated rename groups for the caller to hand off.
- ``enroll_profiles()`` -- slow. WAV conversion + pyannote embedding
  extraction + campaign membership. Called from ``web/jobs.py``'s
  ``_run_enroll_job`` (a ``JOB_ENROLL`` job), with an optional ``progress``
  callback so the job's log stream shows what's happening.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# pyannote's raw speaker-label format. A submitted name matching this is
# never a real display name -- it means the field was left untouched.
RAW_LABEL_RE = re.compile(r"^SPEAKER_\d+$")


def _load_diar_sidecar(md_path: Path) -> Optional[dict]:
    """Load the enrollment sidecar for a transcript, or None if absent/corrupt."""
    import json as _json

    sidecar_path = md_path.with_name(md_path.stem + "_diar.json")
    if not sidecar_path.exists():
        return None
    try:
        return _json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _segment_intervals(segments: list) -> list[tuple[float, float, str]]:
    """Normalise ``DiarizationSegment``-like or plain-dict segments into
    ``(start, end, raw_label)`` tuples for interval matching."""
    intervals: list[tuple[float, float, str]] = []
    for seg in segments:
        if isinstance(seg, dict):
            sp, start, end = seg.get("speaker"), seg.get("start"), seg.get("end")
        else:
            sp = getattr(seg, "speaker", None)
            start = getattr(seg, "start", None)
            end = getattr(seg, "end", None)
        if sp is None or start is None or end is None:
            continue
        intervals.append((float(start), float(end), sp))
    return intervals


def _parse_md_timestamp(ts: str) -> float:
    """Parse a rendered ``MM:SS`` or ``H:MM:SS`` markdown timestamp to seconds.

    Truncated to whole seconds by ``time_utils.format_timestamp`` at render
    time -- this is the source of the imprecision both ``build_legacy_label_map``
    and ``_attribute_block_to_label`` document (F7).
    """
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            return float(int(parts[0]) * 60 + int(parts[1]))
        if len(parts) == 3:
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    except ValueError:
        pass
    return 0.0


def _attribute_block_to_label(
    t: float, intervals: list[tuple[float, float, str]]
) -> tuple[Optional[str], bool]:
    """Attribute one markdown block's timestamp to a raw pyannote label.

    Returns ``(raw_label, confident)``. ``confident`` is True only when *t*
    falls inside a diarization segment's ``[start, end]`` interval exactly;
    False when just the nearest-by-start fallback matched -- whisper segment
    starts routinely fall outside pyannote turns, and rendered timestamps are
    truncated to whole seconds, so the fallback is a real but weaker signal.
    Used per-block (F6) rather than per-label (F7's fragile first-write-wins
    ``setdefault``), so one imprecise block no longer poisons a whole label.
    """
    if not intervals:
        return None, False
    containing = next(((s, e, sp) for s, e, sp in intervals if s <= t <= e), None)
    if containing is not None:
        return containing[2], True
    nearest = min(intervals, key=lambda iv: abs(iv[0] - t))
    return nearest[2], False


def build_legacy_label_map(md_path: Path, segments: list) -> dict[str, str]:
    """Map raw pyannote labels -> current display name in the transcript body.

    LEGACY FALLBACK ONLY (F7) -- used only when a transcript's ``_diar.json``
    sidecar predates the persisted ``speaker_map`` key (see
    ``resolve_current_names``, which is what every caller should go through).

    Reconstructed by matching markdown block timestamps against pyannote
    segment intervals: for each ``**display_name** *(HH:MM:SS)*`` line, find
    the diarization segment whose ``[start, end]`` interval contains that
    timestamp -- the segment's raw label is the one the aligner assigned to
    that whisper line. Falls back to the nearest segment by start time when
    no interval contains the timestamp exactly (whisper segment starts
    routinely fall outside pyannote turns). First-write-wins (``setdefault``)
    across the whole transcript -- fragile if an early block is misattributed
    (see ``_attribute_block_to_label``, which demotes this to a per-block
    decision for the rename path itself).

    Returns raw_label -> display_name for every label that could be matched.
    On a *first* pass (no profiles enrolled yet, body still has raw labels)
    this naturally maps ``SPEAKER_00 -> "SPEAKER_00"`` since the markdown
    itself contains the raw label as the "display name". Callers that use
    this map to prefill a form must filter out those raw-label-valued
    entries themselves (see ``template_current_names``) -- the POST/rename
    path wants the unfiltered map (raw-to-raw is a correct, useful no-op
    target on a first pass).
    """
    import re as _re

    label_map: dict[str, str] = {}
    try:
        intervals = _segment_intervals(segments)
        if not intervals:
            return {}

        md_pattern = _re.compile(
            r"\*\*(.+?)\*\*\s+\*\((\d+:\d{2}(?::\d{2})?)\)\*"
        )
        md_text = md_path.read_text(encoding="utf-8")

        for m in md_pattern.finditer(md_text):
            display = m.group(1)
            if display == "UNKNOWN":
                continue
            t = _parse_md_timestamp(m.group(2))
            raw_label, _confident = _attribute_block_to_label(t, intervals)
            if raw_label is not None:
                label_map.setdefault(raw_label, display)
    except Exception:
        pass
    return label_map


def resolve_current_names(
    md_path: Path, diar: Optional[dict], segments: list
) -> dict[str, str]:
    """Resolve raw_label -> current display name in the transcript body.

    Single entry point (F7) for "what does this raw pyannote label currently
    display as". Resolution order:

    1. The authoritative ``speaker_map`` persisted in the enrollment sidecar
       at transcript-write time (``diar["speaker_map"]``) -- exactly the map
       the formatter used, no reconstruction needed.
    2. ``build_legacy_label_map()``'s interval-matching heuristic, for
       sidecars written before the ``speaker_map`` key existed (or when no
       sidecar dict is available at all).

    ``diar`` is the loaded ``_diar.json`` sidecar dict (or ``None``);
    ``segments`` is the diarization segments to fall back on (accepts either
    ``DiarizationSegment``-like objects or the sidecar's plain-dict form --
    same normalisation as ``build_legacy_label_map``). Every caller that
    needs this resolution -- both wizard GET routes (prefill) and
    ``apply_renames`` (old-name resolution for the rename itself) -- goes
    through this one function.
    """
    if diar:
        speaker_map = diar.get("speaker_map")
        if isinstance(speaker_map, dict) and speaker_map:
            return dict(speaker_map)
    return build_legacy_label_map(md_path, segments)


def template_current_names(current_names: dict[str, str]) -> dict[str, str]:
    """Filter a raw-label-map for template prefill (F2 layer 1).

    ``build_legacy_label_map`` maps ``SPEAKER_00 -> "SPEAKER_00"`` on a first
    pass, before any renames have happened -- the markdown body legitimately
    has the raw label as its only "display name" at that point. Prefilling a
    form input with that value is exactly the F2 bug (submitting untouched
    fields enrolls junk "SPEAKER_XX" profiles), so entries whose value is
    itself raw-label-shaped are dropped here. The wizard template then shows
    an empty input (with the raw label as heading/placeholder only) instead.
    """
    return {k: v for k, v in current_names.items() if not RAW_LABEL_RE.match(v)}


@dataclass
class RenameResult:
    """Outcome of ``apply_renames`` -- the fast, synchronous half of the
    wizard submission.

    ``current_names`` is the (unfiltered) current_names map used to resolve
    renames -- kept for callers/tests that want to inspect it.

    ``groups`` maps *target display name* -> list of raw pyannote labels
    assigned to it, for every rename that is actually eligible for
    enrollment (F2's raw-label guard and F3's unchanged-name-with-existing-
    profile skip have both already been applied). Empty when nothing
    submitted was eligible -- callers use this to decide whether a
    ``JOB_ENROLL`` job is worth enqueueing at all.
    """

    current_names: dict[str, str] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)


def apply_renames(
    md_path: Path,
    segments: list,
    renames: dict[str, str],
    data_dir=None,
) -> RenameResult:
    """Apply a wizard submission's renames to the transcript markdown.

    Synchronous and fast -- this is the part that used to run inline with
    the (now-async) embedding extraction; it stays inline in the HTTP
    request. ``segments`` must be ``DiarizationSegment``-like objects
    (attribute access) -- both callers normalise to that before calling in.

    F6/F7 fix: renames are resolved against the sidecar's authoritative
    ``speaker_map`` when available (``resolve_current_names``), and applied
    to the transcript body in a single pass over the ORIGINAL content rather
    than a sequential loop of global find/replace calls. Each markdown block
    is attributed to a raw pyannote label *once* (by timestamp, falling back
    to an unambiguous name match -- see ``_attribute_block_to_label``), and
    only blocks whose raw label was actually renamed are rewritten
    (``rewrite_transcript_blocks``). This is what makes a same-submit swap
    (Alice<->Bob) and a shared-display-name rename (two raw labels both
    currently "Dan", only one renamed) both come out correct -- neither is
    representable as "replace this string with that string" the way the old
    ``update_speaker_names`` loop required.

    Returns a ``RenameResult`` whose ``groups`` the caller hands to
    ``enroll_profiles()`` -- directly, or (the web routes' choice) via a
    ``JOB_ENROLL`` job so the slow embedding-extraction step doesn't block
    the response.
    """
    import json as _json
    from collections import Counter

    from wisper_transcribe.formatter import parse_transcript_blocks, rewrite_transcript_blocks
    from wisper_transcribe.speaker_manager import load_profiles

    diar = _load_diar_sidecar(md_path)
    current_names = resolve_current_names(md_path, diar, segments)

    # F2: never rename/enroll a submission whose *new* name is itself
    # raw-label-shaped -- that means the field was left untouched.
    valid: dict[str, str] = {
        raw: new for raw, new in renames.items() if not RAW_LABEL_RE.match(new)
    }
    if not valid:
        return RenameResult(current_names, groups={})

    existing_profiles = load_profiles(data_dir)

    old_names: dict[str, str] = {}
    eligible_for_enroll: dict[str, bool] = {}
    for raw, new in valid.items():
        old = current_names.get(raw, raw)
        old_names[raw] = old
        profile_key = new.lower().replace(" ", "_")
        unchanged = old == new
        profile_exists = profile_key in existing_profiles
        # F3: skip the enroll step (not just the rename) when nothing
        # changed and a profile already exists under that name.
        eligible_for_enroll[raw] = not (unchanged and profile_exists)

    # How many raw labels currently display each name -- >1 means a shared
    # display name (legitimate after F3's many-to-one naming). Used both to
    # gate the per-block name-based fallback and to keep the frontmatter
    # rewrite from guessing at an ambiguous shared entry.
    label_counts = Counter(current_names.values())

    content = md_path.read_text(encoding="utf-8")

    if segments:
        intervals = _segment_intervals(segments)
        blocks = parse_transcript_blocks(content)
        updated_speakers: dict[int, str] = {}
        for block in blocks:
            if not block["has_speaker"]:
                continue
            # `include_timestamps=False` (the web upload's "Include
            # timestamps" option) renders blocks as "**Speaker**: text" with
            # no "*(ts)*" at all -- there is no timing signal to attribute
            # from, so go straight to the name-based fallback below rather
            # than attributing against t=0.0 (which would look "confident"
            # purely by accident whenever some segment happens to start at 0).
            raw_label: Optional[str] = None
            confident = False
            if block["timestamp"]:
                t = _parse_md_timestamp(block["timestamp"])
                raw_label, confident = _attribute_block_to_label(t, intervals)
            if not confident:
                # The coarse nearest-timestamp guess is unreliable here --
                # prefer an unambiguous name match instead (exactly one raw
                # label currently displays this block's speaker name). If
                # the name is itself shared (ambiguous), keep whatever the
                # interval match already found (possibly None, if there was
                # no timestamp at all) rather than guessing further -- it's
                # imprecise but the best available signal, and per-block
                # (not per-label) so a bad guess here can't poison anything
                # beyond this one block.
                candidates = [
                    r for r in current_names if current_names[r] == block["speaker"]
                ]
                if len(candidates) == 1:
                    raw_label = candidates[0]
            if raw_label is None or raw_label not in valid:
                continue
            new_name = valid[raw_label]
            if new_name == old_names[raw_label]:
                continue
            updated_speakers[block["index"]] = new_name

        if updated_speakers:
            content = rewrite_transcript_blocks(content, updated_speakers)

    # Frontmatter `speakers:` list -- F11: rewritten via
    # formatter.rewrite_frontmatter_speakers, which parses/re-dumps the YAML
    # (exact-value name matching, so no prefix collision, and quoting is
    # handled by yaml.dump instead of a regex that never matches it). Every
    # pair is applied in one simultaneous pass against the parsed values
    # (F6), so a same-submit swap (Alice<->Bob) or a new name colliding with
    # another entry can't cross-contaminate. Skip any old name shared by
    # more than one raw label -- the frontmatter list has no way to
    # represent "two people, one name", so an ambiguous entry is left alone
    # rather than guessed at.
    frontmatter_renames = {
        old_names[raw]: new
        for raw, new in valid.items()
        if old_names[raw] != new and label_counts.get(old_names[raw], 0) <= 1
    }
    if frontmatter_renames:
        from wisper_transcribe.formatter import rewrite_frontmatter_speakers
        content = rewrite_frontmatter_speakers(content, frontmatter_renames)

    md_path.write_text(content, encoding="utf-8")

    # F7: keep the sidecar's speaker_map authoritative across wizard
    # re-entries -- every raw label submitted (renamed or resubmitted
    # unchanged) gets its current resolved name recorded, so the next GET
    # prefill and the next apply_renames() call both resolve from here
    # instead of re-deriving from rendered markdown.
    if diar is not None:
        updated_map = dict(current_names)
        updated_map.update(valid)
        diar["speaker_map"] = updated_map
        try:
            sidecar_path = md_path.with_name(md_path.stem + "_diar.json")
            sidecar_path.write_text(_json.dumps(diar, indent=2), encoding="utf-8")
        except Exception:
            pass

    if not segments:
        return RenameResult(current_names, groups={})

    # Group eligible raw labels by target display name -- handles two raw
    # labels being assigned the same display name in one submit (F3).
    groups: dict[str, list[str]] = {}
    for raw, new in valid.items():
        if not eligible_for_enroll[raw]:
            continue
        groups.setdefault(new, []).append(raw)

    return RenameResult(current_names, groups=groups)


def enroll_profiles(
    *,
    input_path: Path,
    segments: list,
    groups: dict[str, list[str]],
    campaign_slug: Optional[str],
    device: str,
    data_dir=None,
    progress: Optional[Callable[[str], None]] = None,
) -> None:
    """Convert audio to WAV, extract/merge/enroll embeddings, update campaign.

    This is the slow half of the wizard submission -- WAV conversion (15-30s
    for a long file) and pyannote embedding extraction per speaker (up to 5
    forward passes each). Callers run this off the request thread (the
    ``JOB_ENROLL`` job runner in ``web/jobs.py``), passing ``progress`` so
    status lines land in the job's log stream.

    ``groups`` is ``apply_renames()``'s ``RenameResult.groups`` -- target
    display name -> raw pyannote labels assigned to it. Semantics (F3 EMA
    merge, averaging across raw labels, campaign membership) are unchanged
    from the pre-split ``apply_enrollment_submit``.

    Raises if ``input_path`` doesn't exist or WAV conversion fails; per-group
    enroll/update failures are logged and skipped so one bad speaker doesn't
    abort the rest.
    """
    if not groups:
        return

    def _progress(msg: str) -> None:
        if progress is not None:
            progress(msg)

    from wisper_transcribe.speaker_manager import load_profiles

    existing_profiles = load_profiles(data_dir)

    import numpy as np

    from wisper_transcribe.audio_utils import convert_to_wav
    from wisper_transcribe.speaker_manager import enroll_speaker, extract_embedding, update_embedding

    _progress("Converting audio…")
    wav_path = convert_to_wav(input_path)
    try:
        total = len(groups)
        for i, (display_name, raw_labels) in enumerate(groups.items(), start=1):
            _progress(f"Extracting embedding for {display_name} ({i}/{total})…")
            profile_key = display_name.lower().replace(" ", "_")
            profile_exists = profile_key in existing_profiles
            try:
                if profile_exists:
                    embeddings = [
                        extract_embedding(wav_path, segments, label, device)
                        for label in raw_labels
                    ]
                    avg = embeddings[0] if len(embeddings) == 1 else np.mean(embeddings, axis=0)
                    update_embedding(profile_key, avg, data_dir=data_dir)
                elif len(raw_labels) == 1:
                    enroll_speaker(
                        name=profile_key,
                        display_name=display_name,
                        role="",
                        audio_path=wav_path,
                        segments=segments,
                        speaker_label=raw_labels[0],
                        device=device,
                        data_dir=data_dir,
                    )
                else:
                    embeddings = [
                        extract_embedding(wav_path, segments, label, device)
                        for label in raw_labels
                    ]
                    avg = np.mean(embeddings, axis=0)
                    enroll_speaker(
                        name=profile_key,
                        display_name=display_name,
                        role="",
                        audio_path=wav_path,
                        segments=segments,
                        speaker_label=raw_labels[0],
                        device=device,
                        data_dir=data_dir,
                        embedding=avg,
                    )
            except Exception as exc:
                log.warning("enroll failed for %s: %s", display_name, exc)
                continue

            if campaign_slug:
                try:
                    from wisper_transcribe.campaign_manager import add_member, load_campaigns
                    campaigns = load_campaigns()
                    if (campaign_slug in campaigns
                            and profile_key not in campaigns[campaign_slug].members):
                        add_member(campaign_slug, profile_key)
                except Exception as exc:
                    log.warning(
                        "add_member failed for %s in campaign %s: %s",
                        profile_key, campaign_slug, exc,
                    )
    finally:
        if wav_path != input_path and wav_path.exists():
            wav_path.unlink(missing_ok=True)
