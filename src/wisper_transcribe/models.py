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
