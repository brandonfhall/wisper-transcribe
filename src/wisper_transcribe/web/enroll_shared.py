"""Shared logic for the speaker-enrollment wizard.

Used by both the transcript-centric wizard (``routes/transcripts.py``) and the
job-based wizard (``routes/transcribe.py``) so they can't drift:

1. Resolve each raw pyannote label's *current* display name (the raw label
   disappears from the markdown once names are written in).
2. Never create a profile named after a raw label (``SPEAKER_03``); such a
   profile would compete in every future match.
3. Merge into existing profiles via EMA instead of overwriting, and average
   embeddings when several raw labels get the same name.

The submission is split in two so the slow half can run as a job:

- ``apply_renames()`` — fast, runs in the request; rewrites the transcript.
- ``enroll_profiles()`` — slow, runs in a ``JOB_ENROLL`` job; WAV conversion,
  embedding extraction, campaign membership.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from wisper_transcribe.transcript_store import save_transcript

log = logging.getLogger(__name__)

# pyannote's raw speaker-label format. A submitted name matching this is
# never a real display name -- it means the field was left untouched.
RAW_LABEL_RE = re.compile(r"^SPEAKER_\d+$")

# Names the pipeline assigns on its own. Renaming to one is allowed, but it
# never becomes a profile (it would compete in every future match).
AUTO_NAME_RE = re.compile(r"^(SPEAKER_\d+|Unknown Speaker \d+|Recurring Speaker \d+)$")


def find_excerpt_clip(out_dir: Path, stem: str, candidates: list[str]) -> Optional[Path]:
    """Return the first existing ``<stem>_excerpt_<label>.mp3``, or None.

    ``candidates`` are labels to try in order (raw label, then a legacy
    display-name key). Each is whitelisted to ``[\\w-]`` and then passed through
    the ``os.path.abspath`` + ``startswith`` guard. CodeQL only recognizes the
    ``os.path`` pattern as a sanitizer, so both layers are required.
    """
    import os as _os
    import re as _re

    base_dir = _os.path.abspath(str(out_dir))
    if not base_dir.endswith(_os.sep):
        base_dir += _os.sep
    for cand in candidates:
        safe_label = _re.sub(r"[^\w\-]", "_", cand)
        candidate_abs = _os.path.abspath(
            _os.path.join(base_dir, f"{stem}_excerpt_{safe_label}.mp3")
        )
        if not candidate_abs.startswith(base_dir):
            continue
        if _os.path.exists(candidate_abs):
            return Path(candidate_abs)
    return None


def _load_diar_sidecar(md_path: Path) -> Optional[dict]:
    """The transcript's diarization data (``transcript_store.read_sidecar``),
    or None if it has no sidecar or it's corrupt."""
    from wisper_transcribe.transcript_store import read_sidecar

    try:
        return read_sidecar(md_path)
    except Exception:
        return None


def stored_embeddings(diar: Optional[dict]) -> dict:
    """Per-label embeddings saved with the transcript, in the current
    embedding space; empty when there are none."""
    from wisper_transcribe.speaker_registry import embeddings_from_sidecar

    if diar is None:
        return {}
    return embeddings_from_sidecar(diar) or {}


def audio_available(diar: Optional[dict]) -> bool:
    """Whether the transcript's source audio file still exists."""
    if diar is None:
        return False
    # Path("") is ".", which exists, so an empty path is checked first.
    raw = diar.get("input_path")
    return bool(raw) and Path(raw).is_file()


def enrollable_labels(diar: Optional[dict], labels: list[str]) -> tuple[list[str], list[str]]:
    """Split ``labels`` into ``(enrollable, skipped)``.

    A label enrolls from its saved embedding, or from the source audio when
    that still exists.
    """
    if diar is None:
        return [], list(labels)
    if audio_available(diar):
        return list(labels), []
    stored = stored_embeddings(diar)
    enrollable = [lb for lb in labels if lb in stored]
    return enrollable, [lb for lb in labels if lb not in stored]


def excerpt_candidates(raw_label: str, legacy_label_map: dict) -> list[str]:
    """Excerpt-file label spellings to try for ``raw_label``, in order: the
    raw label, then the display-name key older transcripts used."""
    cands = [re.sub(r"[^\w\-]", "_", raw_label)]
    legacy = legacy_label_map.get(raw_label)
    if legacy:
        cands.append(re.sub(r"[^\w\-]", "_", legacy))
    return cands


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
    """Parse a rendered ``MM:SS`` or ``H:MM:SS`` timestamp to seconds.

    Rendered timestamps are truncated to whole seconds, which limits how
    precisely blocks can be matched to diarization turns.
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
    """Attribute one block's timestamp to a raw pyannote label.

    Returns ``(raw_label, confident)``. ``confident`` is True only when *t*
    falls inside a segment's ``[start, end]``; the nearest-start fallback is
    weaker because whisper starts often fall just outside pyannote turns.
    Applied per block, so one imprecise block can't mislabel a whole speaker.
    """
    if not intervals:
        return None, False
    containing = next(((s, e, sp) for s, e, sp in intervals if s <= t <= e), None)
    if containing is not None:
        return containing[2], True
    nearest = min(intervals, key=lambda iv: abs(iv[0] - t))
    return nearest[2], False


def build_legacy_label_map(md_path: Path, segments: list) -> dict[str, str]:
    """Reconstruct raw label -> display name from the transcript body.

    Fallback for sidecars without a persisted ``speaker_map``; callers should
    use ``resolve_current_names``. Matches each block's timestamp against
    diarization intervals (nearest start if none contains it), first match
    wins per label.

    On a first pass the body still contains raw labels, so this maps
    ``SPEAKER_00 -> "SPEAKER_00"``. Form prefill must filter those out
    (``template_current_names``); the rename path wants the unfiltered map.
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
    """Resolve raw label -> current display name.

    The single entry point for this question. Uses the sidecar's persisted
    ``speaker_map`` when ``diar`` has one (exactly what the formatter used),
    else ``build_legacy_label_map()``.

    ``segments`` may be ``DiarizationSegment``-like objects or the sidecar's
    plain dicts.
    """
    if diar:
        speaker_map = diar.get("speaker_map")
        if isinstance(speaker_map, dict) and speaker_map:
            return dict(speaker_map)
    return build_legacy_label_map(md_path, segments)


def template_current_names(current_names: dict[str, str]) -> dict[str, str]:
    """Drop entries whose value is still a raw label, for form prefill.

    Prefilling an input with ``SPEAKER_00`` would let an untouched field
    enroll a junk profile, so those inputs start empty instead.
    """
    return {k: v for k, v in current_names.items() if not RAW_LABEL_RE.match(v)}


@dataclass
class RenameResult:
    """Result of ``apply_renames``.

    ``current_names``: the unfiltered map used to resolve renames.
    ``groups``: target display name -> raw labels, for renames eligible for
    enrollment. Empty when there is nothing to enroll.
    """

    current_names: dict[str, str] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)


def apply_renames(
    md_path: Path,
    segments: list,
    renames: dict[str, str],
    data_dir=None,
    source: str = "manual",
) -> RenameResult:
    """Apply a wizard submission's renames to the transcript.

    Fast; runs in the request. ``segments`` must be ``DiarizationSegment``-like.

    Rewrites the body in one pass over the original content: each block is
    attributed to a raw label once (``_attribute_block_to_label``) and only
    blocks whose label was renamed change. That handles a same-submit swap
    (Alice<->Bob) and two labels sharing one display name, neither of which a
    global find/replace can express.

    Each changed label's ``speaker_map_source`` becomes ``source``, so the
    campaign relabel pass (``source="auto"``) never overwrites a manual name.

    Returns a ``RenameResult`` whose ``groups`` go to ``enroll_profiles()``.
    """
    import json as _json
    from collections import Counter

    from wisper_transcribe.formatter import parse_transcript_blocks, rewrite_transcript_blocks
    from wisper_transcribe.speaker_manager import load_profiles

    diar = _load_diar_sidecar(md_path)
    current_names = resolve_current_names(md_path, diar, segments)

    # A raw-label-shaped new name means the field was left untouched.
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
        # Unchanged name with an existing profile: rename only, no enroll.
        eligible_for_enroll[raw] = not (unchanged and profile_exists) and not AUTO_NAME_RE.match(new)

    # Count of raw labels per current display name; >1 means a shared name,
    # which disables the name-based fallback and the frontmatter rewrite.
    label_counts = Counter(current_names.values())

    content = md_path.read_text(encoding="utf-8")

    if segments:
        intervals = _segment_intervals(segments)
        blocks = parse_transcript_blocks(content)
        updated_speakers: dict[int, str] = {}
        for block in blocks:
            if not block["has_speaker"]:
                continue
            # The block's displayed name is the strongest signal: timestamps are
            # whole seconds and overlapping turns make them land in another
            # speaker's turn. Timing only separates labels sharing one name
            # (or places a block whose name isn't in the map).
            raw_label: Optional[str] = None
            candidates = [
                r for r in current_names if current_names[r] == block["speaker"]
            ]
            if len(candidates) == 1:
                raw_label = candidates[0]
            elif block["timestamp"]:
                t = _parse_md_timestamp(block["timestamp"])
                pool = [iv for iv in intervals if iv[2] in candidates] if candidates else intervals
                raw_label, _confident = _attribute_block_to_label(t, pool)
            if raw_label is None or raw_label not in valid:
                continue
            new_name = valid[raw_label]
            if new_name == old_names[raw_label]:
                continue
            updated_speakers[block["index"]] = new_name

        if updated_speakers:
            content = rewrite_transcript_blocks(content, updated_speakers)

    # Rewrite frontmatter `speakers:` via YAML round-trip in one simultaneous
    # pass (safe for swaps and quoted names). Names shared by several raw
    # labels are skipped: the list can't represent two people with one name.
    frontmatter_renames = {
        old_names[raw]: new
        for raw, new in valid.items()
        if old_names[raw] != new and label_counts.get(old_names[raw], 0) <= 1
    }
    if frontmatter_renames:
        from wisper_transcribe.formatter import rewrite_frontmatter_speakers
        content = rewrite_frontmatter_speakers(content, frontmatter_renames)

    save_transcript(md_path, content)

    # Record every submitted label's current name so the sidecar stays
    # authoritative for the next visit.
    if diar is not None:
        updated_map = dict(current_names)
        updated_map.update(valid)
        diar["speaker_map"] = updated_map
        sources = dict(diar.get("speaker_map_source") or {})
        for raw, new in valid.items():
            if new != old_names[raw]:
                sources[raw] = source
        diar["speaker_map_source"] = sources
        # The .md is rewritten first (above), then the speaker rows. A crash
        # in between leaves the rows stale; interval matching repairs that.
        try:
            from wisper_transcribe.transcript_store import set_speaker_names
            set_speaker_names(md_path, updated_map, sources)
        except Exception:
            log.warning("Could not record speaker names for %s", md_path.name, exc_info=True)

    if not segments:
        return RenameResult(current_names, groups={})

    # Group eligible raw labels by target name (several labels may share one).
    groups: dict[str, list[str]] = {}
    for raw, new in valid.items():
        if not eligible_for_enroll[raw]:
            continue
        groups.setdefault(new, []).append(raw)

    return RenameResult(current_names, groups=groups)


def enroll_profiles(
    *,
    input_path: Optional[Path] = None,
    segments: list,
    groups: dict[str, list[str]],
    campaign_slug: Optional[str],
    device: str,
    data_dir=None,
    progress: Optional[Callable[[str], None]] = None,
    stored: Optional[dict] = None,
    md_path: Optional[Path] = None,
) -> list[str]:
    """Enroll or EMA-update each group's profile; returns the skipped names.

    Runs in a ``JOB_ENROLL`` job with ``progress`` feeding the job log.
    ``groups`` is ``RenameResult.groups``. A label's vector comes from
    ``stored`` (embeddings saved with the transcript) when present; otherwise
    it is extracted from ``input_path``, converted once and only if needed.
    A group with a label that has neither is skipped whole. Existing profiles
    are EMA-merged; several raw labels for one name are averaged. With
    ``md_path``, a new profile's reference clip is copied from the
    transcript's excerpt. Each profile is added to the campaign.

    Raises if a needed conversion fails; a single group's failure is logged
    and skipped.
    """
    skipped: list[str] = []
    if not groups:
        return skipped

    def _progress(msg: str) -> None:
        if progress is not None:
            progress(msg)

    from wisper_transcribe.speaker_manager import load_profiles

    existing_profiles = load_profiles(data_dir)

    import numpy as np

    from wisper_transcribe.speaker_manager import enroll_speaker, extract_embedding, update_embedding

    stored = stored or {}
    wav_path: Optional[Path] = None

    def _wav() -> Path:
        nonlocal wav_path
        if wav_path is None:
            from wisper_transcribe.audio_utils import convert_to_wav

            _progress("Converting audio…")
            wav_path = convert_to_wav(input_path)
        return wav_path

    def _vector(label: str):
        if label in stored:
            return stored[label]
        return extract_embedding(_wav(), segments, label, device)

    legacy_map: Optional[dict] = None

    def _clip_source(first_label: str) -> Optional[Path]:
        nonlocal legacy_map
        if md_path is None:
            return None
        if legacy_map is None:
            try:
                legacy_map = build_legacy_label_map(md_path, segments)
            except Exception:
                legacy_map = {}
        return find_excerpt_clip(
            md_path.parent, md_path.stem, excerpt_candidates(first_label, legacy_map)
        )

    try:
        total = len(groups)
        for i, (display_name, raw_labels) in enumerate(groups.items(), start=1):
            if input_path is None and not all(lb in stored for lb in raw_labels):
                _progress(
                    f"Skipped {display_name}: no saved voice data and the source audio is gone"
                )
                skipped.append(display_name)
                continue
            if any(lb not in stored for lb in raw_labels):
                _wav()  # a failed conversion aborts the job
                _progress(f"Extracting embedding for {display_name} ({i}/{total})…")
            else:
                _progress(f"Enrolling {display_name} from saved voice data ({i}/{total})…")
            profile_key = display_name.lower().replace(" ", "_")
            profile_exists = profile_key in existing_profiles
            try:
                if profile_exists:
                    embeddings = [_vector(label) for label in raw_labels]
                    avg = embeddings[0] if len(embeddings) == 1 else np.mean(embeddings, axis=0)
                    update_embedding(profile_key, avg, data_dir=data_dir)
                else:
                    extra: dict = {}
                    if md_path is not None:
                        extra = {
                            "clip_source": _clip_source(raw_labels[0]),
                            "source_name": md_path.stem,
                        }
                    if len(raw_labels) == 1 and raw_labels[0] not in stored:
                        enroll_speaker(
                            name=profile_key,
                            display_name=display_name,
                            role="",
                            audio_path=wav_path,
                            segments=segments,
                            speaker_label=raw_labels[0],
                            device=device,
                            data_dir=data_dir,
                            **extra,
                        )
                    else:
                        embeddings = [_vector(label) for label in raw_labels]
                        avg = embeddings[0] if len(embeddings) == 1 else np.mean(embeddings, axis=0)
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
                            **extra,
                        )
            except InterruptedError:
                raise  # Stop: not one failed profile
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
        if wav_path is not None and wav_path != input_path and wav_path.exists():
            wav_path.unlink(missing_ok=True)
    return skipped
