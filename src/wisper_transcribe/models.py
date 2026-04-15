from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TranscriptionSegment:
    start: float
    end: float
    text: str


@dataclass
class DiarizationSegment:
    start: float
    end: float
    speaker: str


@dataclass
class AlignedSegment:
    start: float
    end: float
    speaker: str
    text: str


@dataclass
class SpeakerProfile:
    name: str
    display_name: str
    role: str
    embedding_path: Path
    enrolled_date: str
    enrollment_source: str
    notes: str = ""


# ---------------------------------------------------------------------------
# LLM post-processing (wisper refine / wisper summarize)
# ---------------------------------------------------------------------------


@dataclass
class Edit:
    """A single vocabulary substitution proposed by the LLM and approved by the
    edit-distance guard. `original` and `corrected` are substrings that appear
    verbatim in a transcript body line; `reason` is a short human-readable note.
    """
    original: str
    corrected: str
    reason: str = ""


@dataclass
class SpeakerSuggestion:
    """A suggestion to resolve an `Unknown Speaker N` label to a known profile.

    Never auto-applied regardless of confidence — surfaced in dry-run output
    or in the summary's `## Unresolved Speakers` section for manual review.
    """
    line_idx: int
    current_label: str
    suggested_name: str
    confidence: float
    reason: str = ""


@dataclass
class LootChange:
    """A loot / inventory change mentioned during a session."""
    item: str
    quantity: str = ""           # free-text ("3", "+120 gp", "a few")
    recipient: str = ""          # player/character that gained or lost the item
    note: str = ""


@dataclass
class NPCMention:
    """A non-player character referenced in the session."""
    name: str
    role: str = ""               # "villager", "dragon", "innkeeper"
    first_mentioned_at: str = "" # transcript timestamp string (e.g. "14:22")
    description: str = ""


@dataclass
class SummaryNote:
    """Structured campaign-notes output for a single session."""
    summary: str
    loot: list = field(default_factory=list)           # list[LootChange]
    npcs: list = field(default_factory=list)           # list[NPCMention]
    followups: list = field(default_factory=list)      # list[str]
    unresolved_speakers: list = field(default_factory=list)  # list[SpeakerSuggestion]
    session_title: str = ""
    source_transcript: str = ""
    generated_at: str = ""
    provider: str = ""
    model: str = ""
    refined: bool = False
