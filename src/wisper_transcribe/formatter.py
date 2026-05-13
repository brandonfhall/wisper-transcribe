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


def update_speaker_names(content: str, old_name: str, new_name: str) -> str:
    """Replace all occurrences of a speaker name in an existing markdown transcript."""
    import re

    # Replace in bold speaker labels: **OldName**
    content = re.sub(
        rf"\*\*{re.escape(old_name)}\*\*",
        f"**{new_name}**",
        content,
    )
    # Replace in YAML frontmatter speaker list
    content = re.sub(
        rf"(- name: ){re.escape(old_name)}",
        rf"\g<1>{new_name}",
        content,
    )
    return content
