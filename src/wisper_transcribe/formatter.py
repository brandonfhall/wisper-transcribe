from __future__ import annotations

from typing import Optional

import yaml

from .models import AlignedSegment, TranscriptionSegment
from .time_utils import format_timestamp as _format_timestamp


def _merge_consecutive(segments: list, speaker_map: Optional[dict[str, str]]) -> list[dict]:
    """Merge consecutive segments from the same speaker into one block."""
    merged = []
    for seg in segments:
        if hasattr(seg, "speaker"):
            speaker_raw = seg.speaker
            speaker = speaker_map.get(speaker_raw, speaker_raw) if speaker_map else speaker_raw
        else:
            speaker = None

        text = seg.text.strip()
        if not text:
            continue

        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] += " " + text
            merged[-1]["end"] = seg.end
        else:
            merged.append({"speaker": speaker, "text": text, "start": seg.start, "end": seg.end})

    return merged


def to_markdown(
    segments: list,
    speaker_map: Optional[dict[str, str]],
    metadata: dict,
    include_timestamps: bool = True,
) -> str:
    """Produce markdown transcript from segments.

    segments: list of AlignedSegment or TranscriptionSegment
    speaker_map: maps raw speaker labels to display names (None = no speaker labels)
    metadata: dict with keys title, source_file, date_processed, duration, speakers
    """
    lines = []

    # YAML frontmatter
    frontmatter = {
        "title": metadata.get("title", ""),
        "source_file": metadata.get("source_file", ""),
        "date_processed": metadata.get("date_processed", ""),
        "duration": metadata.get("duration", ""),
    }
    if metadata.get("speakers"):
        frontmatter["speakers"] = metadata["speakers"]
    if metadata.get("job_id"):
        frontmatter["job_id"] = metadata["job_id"]

    lines.append("---")
    lines.append(yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).rstrip())
    lines.append("---")
    lines.append("")

    title = metadata.get("title", "Transcript")
    lines.append(f"# {title}")
    lines.append("")

    merged = _merge_consecutive(segments, speaker_map)

    for block in merged:
        speaker = block["speaker"]
        text = block["text"]
        ts = _format_timestamp(block["start"])

        if speaker and speaker_map is not None:
            if include_timestamps:
                lines.append(f"**{speaker}** *({ts})*: {text}")
            else:
                lines.append(f"**{speaker}**: {text}")
        else:
            if include_timestamps:
                lines.append(f"*({ts})* {text}")
            else:
                lines.append(text)
        lines.append("")

    from . import __version__

    lines.append("---")
    lines.append(f"*Transcribed by wisper-transcribe v{__version__}*")

    return "\n".join(lines)


def parse_transcript_blocks(body: str) -> list[dict]:
    """Parse a markdown transcript body into structured speaker blocks.

    Returns a list of dicts with keys: index, speaker, timestamp, text, has_speaker.
    Lines that are headings, horizontal rules, or the footer are skipped.

    The inline ``*(HH:MM:SS)*`` timestamp is optional -- ``to_markdown()``
    omits it entirely when called with ``include_timestamps=False`` (the web
    upload form's "Include timestamps" checkbox), producing ``**Speaker**:
    text`` instead of ``**Speaker** *(ts)*: text``. ``timestamp`` is ``''``
    for those blocks; callers that need per-block timing (e.g.
    ``enroll_shared.apply_renames``'s raw-label attribution) must handle an
    empty timestamp themselves -- there is no timing signal to fall back on.
    """
    import re

    speaker_re = re.compile(r'^\*\*(.+?)\*\*\s*(?:\*\((.+?)\)\*)?:\s*(.*)')
    no_speaker_re = re.compile(r'^\*\((.+?)\)\*\s*(.*)')

    blocks = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or stripped == '---':
            continue
        m = speaker_re.match(stripped)
        if m:
            blocks.append({
                'index': len(blocks),
                'speaker': m.group(1),
                'timestamp': m.group(2) or '',
                'text': m.group(3),
                'has_speaker': True,
            })
            continue
        m = no_speaker_re.match(stripped)
        if m:
            blocks.append({
                'index': len(blocks),
                'speaker': '',
                'timestamp': m.group(1),
                'text': m.group(2),
                'has_speaker': False,
            })
    return blocks


def rewrite_transcript_blocks(content: str, updated_speakers: dict) -> str:
    """Apply per-block speaker name changes to a full markdown transcript.

    updated_speakers maps block_index (int) to the new speaker name (str).
    Only lines matching the speaker-block pattern are counted; all other lines
    are passed through unchanged. Returns the full updated markdown string.

    Matches both the timestamped (``**Speaker** *(ts)*: text``) and
    timestamp-free (``**Speaker**: text``, ``include_timestamps=False``)
    formats -- see ``parse_transcript_blocks``.
    """
    import re

    speaker_line_re = re.compile(r'^\*\*(.+?)\*\*(\s*(?:\*\(.+?\)\*)?:.*)')

    block_idx = 0
    new_lines = []
    for line in content.splitlines():
        m = speaker_line_re.match(line.strip()) if line.strip() else None
        if m:
            if block_idx in updated_speakers:
                raw = updated_speakers[block_idx]
                new_speaker = str(raw).strip().replace('\n', '').replace('\r', '')
                new_lines.append(f'**{new_speaker}**{m.group(2)}')
            else:
                new_lines.append(line.strip())
            block_idx += 1
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)


def rewrite_frontmatter_speakers(content: str, old_to_new: dict[str, str]) -> str:
    """Rewrite ``speakers:`` frontmatter entries by parsing and re-dumping YAML.

    F11: two previous regex-based approaches (this function replaces both)
    shared the same defects -- an un-anchored ``- name: Dan`` pattern
    corrupts ``- name: Dan Smith`` (prefix collision), and ``yaml.dump``
    quotes names with special characters (e.g. ``- name: 'O''Brien'``) which
    an unquoted regex never matches, silently leaving the frontmatter stale.

    This function instead parses the frontmatter as YAML and matches names
    as whole values: for every entry in ``speakers`` that is a dict with a
    ``name`` key, if that name is an EXACT key in ``old_to_new`` it is
    replaced. Exact-value matching means no prefix collision is possible,
    and going through ``yaml.safe_load``/``yaml.dump`` means quoting is
    handled correctly regardless of what characters the name contains.

    All renames in ``old_to_new`` are applied in one pass against the
    parsed (not re-serialized) values, so a same-submit swap (Alice<->Bob)
    or any other set of simultaneous renames comes out correct -- same
    property the F6 fix established for the per-block body rewrite.

    Returns ``content`` unchanged if it doesn't start with a ``---``
    frontmatter block, if the closing ``---`` is missing, if the
    frontmatter fails to parse as YAML, or if there is no ``speakers`` list
    in it. The document body (everything after the closing ``---``) is
    preserved byte-for-byte.

    Note: re-dumping via ``yaml.dump`` may normalize unrelated frontmatter
    formatting (key order, quoting style) even for keys that weren't
    renamed -- that's an accepted side effect of round-tripping through the
    YAML parser instead of doing surgical text edits.
    """
    if not content.startswith("---"):
        return content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return content

    _prefix, raw_frontmatter, body = parts

    try:
        frontmatter = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError:
        return content

    if not isinstance(frontmatter, dict):
        return content

    speakers = frontmatter.get("speakers")
    if not isinstance(speakers, list):
        return content

    changed = False
    for entry in speakers:
        if isinstance(entry, dict) and "name" in entry:
            old = entry["name"]
            if old in old_to_new:
                entry["name"] = old_to_new[old]
                changed = True

    if not changed:
        return content

    dumped = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    return f"---\n{dumped}---{body}"


def update_speaker_names(content: str, old_name: str, new_name: str) -> str:
    """Replace all occurrences of a speaker name in an existing markdown transcript.

    R32-7 warning: the `**OldName**` regex matches ANY bold text equal to
    `old_name`, not just genuine `**Speaker**` block headers -- if `old_name`
    also happens to appear as bold body text elsewhere in the transcript
    (coincidentally, not as a speaker label), that occurrence is rewritten
    too. This is a known, accepted limitation (not fixed here); callers that
    need header-only precision should go through
    `rewrite_transcript_blocks()` instead, which operates on parsed
    per-block indices rather than a body-wide regex.
    """
    import re

    # Replace in bold speaker labels: **OldName**
    content = re.sub(
        rf"\*\*{re.escape(old_name)}\*\*",
        f"**{new_name}**",
        content,
    )
    # Replace in YAML frontmatter speaker list (F11: parse/re-dump YAML
    # instead of a regex, so a rename target name isn't corrupted by a
    # prefix collision and quoted names are matched correctly).
    content = rewrite_frontmatter_speakers(content, {old_name: new_name})
    return content
