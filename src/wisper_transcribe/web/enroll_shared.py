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
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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


def build_legacy_label_map(md_path: Path, segments: list) -> dict[str, str]:
    """Map raw pyannote labels -> current display name in the transcript body.

    Reconstructed by matching markdown block timestamps against pyannote
    segment intervals: for each ``**display_name** *(HH:MM:SS)*`` line, find
    the diarization segment whose ``[start, end]`` interval contains that
    timestamp -- the segment's raw label is the one the aligner assigned to
    that whisper line. Falls back to the nearest segment by start time when
    no interval contains the timestamp exactly (whisper segment starts
    routinely fall outside pyannote turns).

    Returns raw_label -> display_name for every label that could be matched.
    On a *first* pass (no profiles enrolled yet, body still has raw labels)
    this naturally maps ``SPEAKER_00 -> "SPEAKER_00"`` since the markdown
    itself contains the raw label as the "display name". Callers that use
    this map to prefill a form must filter out those raw-label-valued
    entries themselves (see ``resolve_current_names_for_template``) --
    the POST/rename path wants the unfiltered map (raw-to-raw is a correct,
    useful no-op target on a first pass).
    """
    import re as _re

    label_map: dict[str, str] = {}
    try:
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
            parts = m.group(2).split(":")
            if len(parts) == 2:
                t_sec = int(parts[0]) * 60 + int(parts[1])
            else:
                t_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            t = float(t_sec)
            containing = next(
                ((s, e, sp) for s, e, sp in intervals if s <= t <= e),
                None,
            )
            if containing is None:
                containing = min(intervals, key=lambda iv: abs(iv[0] - t))
            raw_label = containing[2]
            label_map.setdefault(raw_label, display)
    except Exception:
        pass
    return label_map


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
class EnrollmentResult:
    """Outcome of ``apply_enrollment_submit``, for callers/tests that need to
    tell "renamed + enrolled" apart from "renamed only, audio missing" (F5).

    ``current_names`` is the (unfiltered) current_names map used to resolve
    renames -- kept for callers/tests that want to inspect it, same as the
    plain dict this function used to return.

    ``audio_missing`` is True only when there was at least one rename that
    *would* have triggered an enroll/update step (i.e. renaming actually
    happened and something was eligible for enrollment) but the source audio
    file could not be found on disk. Routes use this to surface a notice
    instead of a silent success redirect.
    """

    current_names: dict[str, str] = field(default_factory=dict)
    audio_missing: bool = False


def apply_enrollment_submit(
    *,
    md_path: Path,
    segments: list,
    input_path: Path,
    campaign_slug: Optional[str],
    device: str,
    renames: dict[str, str],
    data_dir=None,
) -> EnrollmentResult:
    """Apply a wizard submission: rename in the transcript, enroll/update profiles.

    ``segments`` must be ``DiarizationSegment``-like objects (attribute
    access) -- both callers normalise to that before calling in.

    Returns an ``EnrollmentResult`` -- see its docstring for what
    ``audio_missing`` means and why callers care.
    """
    from wisper_transcribe.formatter import update_speaker_names
    from wisper_transcribe.speaker_manager import load_profiles

    current_names = build_legacy_label_map(md_path, segments)

    # F2: never rename/enroll a submission whose *new* name is itself
    # raw-label-shaped -- that means the field was left untouched.
    valid: dict[str, str] = {
        raw: new for raw, new in renames.items() if not RAW_LABEL_RE.match(new)
    }
    if not valid:
        return EnrollmentResult(current_names, audio_missing=False)

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

    # --- Rename: single pass against the content read once, like before ---
    content = md_path.read_text(encoding="utf-8")
    for raw, new in valid.items():
        old = old_names[raw]
        if old == new:
            continue
        content = update_speaker_names(content, old, new)
    md_path.write_text(content, encoding="utf-8")

    if not segments:
        return EnrollmentResult(current_names, audio_missing=False)

    # Group eligible raw labels by target display name -- handles two raw
    # labels being assigned the same display name in one submit (F3).
    groups: dict[str, list[str]] = {}
    for raw, new in valid.items():
        if not eligible_for_enroll[raw]:
            continue
        groups.setdefault(new, []).append(raw)

    if not groups:
        return EnrollmentResult(current_names, audio_missing=False)

    # F5: only *now* -- once we know there's actually something eligible to
    # enroll -- do we check for audio. Checking earlier would flag
    # audio_missing even when every rename was a no-op that wouldn't have
    # touched the audio anyway.
    if not input_path.exists():
        log.warning("Enrollment skipped: source audio not found at %s", input_path)
        return EnrollmentResult(current_names, audio_missing=True)

    import numpy as np

    from wisper_transcribe.audio_utils import convert_to_wav
    from wisper_transcribe.speaker_manager import enroll_speaker, extract_embedding, update_embedding

    wav_path = convert_to_wav(input_path)
    try:
        for display_name, raw_labels in groups.items():
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

    return EnrollmentResult(current_names, audio_missing=False)
