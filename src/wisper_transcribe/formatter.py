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
    """Parse a transcript body into speaker blocks.

    Returns dicts with index, speaker, timestamp, text, has_speaker. Headings,
    rules, and the footer are skipped. ``timestamp`` is ``''`` for blocks
    rendered without timestamps (``**Speaker**: text``); callers needing timing
    must handle that.
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
    """Apply per-block speaker renames to a full transcript.

    ``updated_speakers`` maps block index to new name. Handles both the
    timestamped and timestamp-free block formats; all other lines pass through.
    Returns the updated markdown.
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
    """Rename ``speakers:`` frontmatter entries via a YAML round-trip.

    Names are matched as exact values, so ``Dan`` never matches ``Dan Smith``
    and quoted names (``'O''Brien'``) work. All renames apply in one pass
    against the parsed values, so swaps (Alice<->Bob) are correct.

    Returns ``content`` unchanged when there is no frontmatter, it doesn't
    parse, or it has no ``speakers`` list. The body is preserved byte-for-byte;
    re-dumping may normalise unrelated frontmatter formatting.
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
    """Replace every ``**old_name**`` in a transcript.

    Also matches bold body text equal to ``old_name``, not just speaker
    headers. Use ``rewrite_transcript_blocks()`` for header-only precision.
    """
    import re

    # Replace in bold speaker labels: **OldName**
    content = re.sub(
        rf"\*\*{re.escape(old_name)}\*\*",
        f"**{new_name}**",
        content,
    )
    # Frontmatter speaker list via YAML round-trip (exact, quote-safe).
    content = rewrite_frontmatter_speakers(content, {old_name: new_name})
    return content
