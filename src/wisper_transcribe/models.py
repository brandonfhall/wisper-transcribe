from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
# Campaign management
# ---------------------------------------------------------------------------


@dataclass
class CampaignMember:
    """A speaker profile's membership in a specific campaign, with per-campaign overrides."""
    profile_key: str
    role: str = ""
    character: str = ""


@dataclass
class Campaign:
    """A named campaign (game session series) with a roster of enrolled speakers."""
    slug: str
    display_name: str
    created: str
    members: dict = field(default_factory=dict)       # dict[str, CampaignMember]
    transcripts: list = field(default_factory=list)   # list[str] — transcript stems


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


# ---------------------------------------------------------------------------
# Discord recording bot
# ---------------------------------------------------------------------------


@dataclass
class SegmentRecord:
    """One completed or in-progress segment file within a recording stream."""
    index: int                 # monotonic per stream, 0-based
    stream: str                # "mixed" or a discord_user_id
    started_at: str            # ISO 8601 datetime string (UTC)
    duration_s: float          # wall-clock duration of this segment
    path: str                  # relative path from the recording root directory
    finalized: bool            # True once the EOS page has been flushed


@dataclass
class RejoinAttempt:
    """One Discord voice reconnect attempt logged by BotManager."""
    timestamp: str             # ISO 8601 datetime string (UTC)
    close_code: int            # Discord close code that triggered the reconnect
    attempt_number: int        # 1-based retry number within this session


@dataclass
class Recording:
    """Metadata for a single Discord voice recording session."""
    id: str                          # uuid4
    voice_channel_id: str
    guild_id: str
    started_at: str                  # ISO 8601 datetime string (UTC)
    status: str                      # recording|degraded|completed|failed|transcribing|transcribed
    discord_speakers: dict           # discord_user_id → wisper profile name (or "")
    segment_manifest: list           # list[SegmentRecord]
    rejoin_log: list                 # list[RejoinAttempt]
    campaign_slug: Optional[str] = None
    ended_at: Optional[str] = None   # ISO 8601 datetime string (UTC)
    combined_path: Optional[str] = None   # absolute path to final/combined.wav (post-stop)
    per_user_dir: Optional[str] = None    # absolute path to per-user/ directory
    transcript_path: Optional[str] = None
    notes: str = ""
