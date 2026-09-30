"""Campaign relabel pass: sidecar embeddings, provenance, recurring unknowns."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from tests._seed import save_campaigns as _seed_save_campaigns

from wisper_transcribe.config import EMBEDDING_SPACE
from wisper_transcribe.formatter import to_markdown
from wisper_transcribe.models import AlignedSegment

from ._seed import seed_profile, seed_sidecar, sidecar_data

ALICE = np.array([1.0, 0.0, 0.0, 0.0])
BOB = np.array([0.0, 1.0, 0.0, 0.0])
GUEST = np.array([0.0, 0.0, 1.0, 0.0])
OTHER = np.array([0.0, 0.0, 0.0, 1.0])


def _profile(data_dir: Path, key: str, emb: np.ndarray) -> None:
    seed_profile(key, key.title(), data_dir=data_dir, embedding=emb)


def _transcript(out_dir: Path, stem: str, names: dict[str, str], embeddings=None,
                sources=None, input_path: str = "") -> Path:
    """Write <stem>.md plus sidecar; each label speaks one 10 s block in label order."""
    labels = sorted(names)
    segs = [AlignedSegment(i * 10.0, i * 10.0 + 9.0, label, f"line from {label}")
            for i, label in enumerate(labels)]
    md = to_markdown(segs, speaker_map=names,
                     metadata={"title": stem, "source_file": "x.mp3", "date_processed": "2026-01-01",
                               "duration": "0:01:00",
                               "speakers": [{"name": n, "role": ""} for n in dict.fromkeys(names.values())]})
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    diar = {
        "input_path": input_path,
        "campaign": "game",
        "diarization_segments": [{"start": s.start, "end": s.end, "speaker": s.speaker} for s in segs],
        "speaker_map": dict(names),
    }
    if sources is not None:
        diar["speaker_map_source"] = sources
    if embeddings is not None:
        diar["embedding_space"] = EMBEDDING_SPACE
        diar["speaker_embeddings"] = {k: v.tolist() for k, v in embeddings.items()}
    seed_sidecar(md_path, diar)
    return md_path


@pytest.fixture
def world(tmp_path, monkeypatch):
    import wisper_transcribe.campaign_manager as cm

    data = tmp_path / "data"
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_DATA_DIR", str(data))
    cm.create_campaign("Game", data_dir=data)
    for key, emb in (("alice", ALICE), ("bob", BOB)):
        _profile(data, key, emb)
        cm.add_member("game", key, data_dir=data)
    return data, out


def _add(data: Path, stem: str) -> None:
    import wisper_transcribe.campaign_manager as cm
    cm.move_transcript_to_campaign(stem, "game", data_dir=data)


def _run(data, out, **kw):
    from wisper_transcribe.speaker_registry import relabel_campaign
    return relabel_campaign("game", output_dir=out, data_dir=data, threshold=0.55, **kw)


def test_auto_unknown_gets_newly_enrolled_profile(world):
    data, out = world
    md = _transcript(out, "s1", {"SPEAKER_00": "Unknown Speaker 1", "SPEAKER_01": "Bob"},
                     embeddings={"SPEAKER_00": ALICE, "SPEAKER_01": BOB},
                     sources={"SPEAKER_00": "auto", "SPEAKER_01": "auto"})
    _add(data, "s1")

    report = _run(data, out)

    assert report.transcripts[0].renamed == {"SPEAKER_00": ("Unknown Speaker 1", "Alice")}
    text = md.read_text(encoding="utf-8")
    assert "**Alice**" in text and "Unknown Speaker 1" not in text
    sidecar = sidecar_data(out / "s1_diar.json")
    assert sidecar["speaker_map"]["SPEAKER_00"] == "Alice"
    assert sidecar["speaker_map_source"]["SPEAKER_00"] == "auto"


def test_manual_names_are_never_overwritten(world):
    data, out = world
    md = _transcript(out, "s1", {"SPEAKER_00": "Carol"},
                     embeddings={"SPEAKER_00": ALICE}, sources={"SPEAKER_00": "manual"})
    _add(data, "s1")

    report = _run(data, out)

    assert report.transcripts[0].renamed == {}
    assert "**Carol**" in md.read_text(encoding="utf-8")


def test_legacy_sidecar_real_name_treated_as_manual(world):
    """No provenance recorded: a real name may have been typed, so keep it."""
    data, out = world
    _transcript(out, "s1", {"SPEAKER_00": "Carol", "SPEAKER_01": "Unknown Speaker 1"},
                embeddings={"SPEAKER_00": ALICE, "SPEAKER_01": BOB})
    _add(data, "s1")

    report = _run(data, out)

    assert report.transcripts[0].renamed == {"SPEAKER_01": ("Unknown Speaker 1", "Bob")}


def test_unknown_voice_in_two_sessions_becomes_recurring(world):
    data, out = world
    for stem in ("s1", "s2"):
        _transcript(out, stem, {"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"},
                    embeddings={"SPEAKER_00": ALICE, "SPEAKER_01": GUEST},
                    sources={"SPEAKER_00": "auto", "SPEAKER_01": "auto"})
        _add(data, stem)
    _transcript(out, "s3", {"SPEAKER_00": "Unknown Speaker 1"},
                embeddings={"SPEAKER_00": OTHER}, sources={"SPEAKER_00": "auto"})
    _add(data, "s3")

    report = _run(data, out)

    assert report.recurring == 1
    by_stem = {t.stem: t for t in report.transcripts}
    assert by_stem["s1"].renamed == {"SPEAKER_01": ("Unknown Speaker 1", "Recurring Speaker 1")}
    assert by_stem["s2"].renamed == {"SPEAKER_01": ("Unknown Speaker 1", "Recurring Speaker 1")}
    assert by_stem["s3"].renamed == {}  # heard once: keeps per-file numbering


def test_dry_run_writes_nothing(world):
    data, out = world
    md = _transcript(out, "s1", {"SPEAKER_00": "Unknown Speaker 1"},
                     embeddings={"SPEAKER_00": ALICE}, sources={"SPEAKER_00": "auto"})
    _add(data, "s1")
    before_md = md.read_text(encoding="utf-8")
    before_sidecar = (out / "s1_diar.json").read_text()

    report = _run(data, out, dry_run=True)

    assert report.transcripts[0].renamed == {"SPEAKER_00": ("Unknown Speaker 1", "Alice")}
    assert md.read_text(encoding="utf-8") == before_md
    assert (out / "s1_diar.json").read_text() == before_sidecar


def test_backfills_embeddings_from_durable_audio(world):
    data, out = world
    audio = out / "s1.wav"  # durable copies live next to the transcript
    audio.write_bytes(b"fake")
    _transcript(out, "s1", {"SPEAKER_00": "Unknown Speaker 1"}, input_path=str(audio))
    _add(data, "s1")

    with patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=audio), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=ALICE) as mock_extract:
        report = _run(data, out)

    mock_extract.assert_called_once()
    assert report.transcripts[0].renamed == {"SPEAKER_00": ("Unknown Speaker 1", "Alice")}
    sidecar = sidecar_data(out / "s1_diar.json")
    assert sidecar["embedding_space"] == EMBEDDING_SPACE
    assert sidecar["speaker_embeddings"]["SPEAKER_00"] == pytest.approx(ALICE.tolist())


def test_skips_transcript_without_embeddings_or_audio(world):
    data, out = world
    _transcript(out, "s1", {"SPEAKER_00": "Unknown Speaker 1"}, input_path="/gone.mp3")
    _add(data, "s1")

    report = _run(data, out)

    assert report.transcripts[0].skipped == "no stored voice data and the source audio is gone"


def test_sidecar_embeddings_from_other_model_are_ignored():
    from wisper_transcribe.speaker_registry import embeddings_from_sidecar

    assert embeddings_from_sidecar({"embedding_space": "old", "speaker_embeddings": {"A": [1.0]}}) is None
    assert embeddings_from_sidecar({"speaker_embeddings": {"A": [1.0]}}) is None
    got = embeddings_from_sidecar({"embedding_space": EMBEDDING_SPACE, "speaker_embeddings": {"A": [1.0, 2.0]}})
    np.testing.assert_array_equal(got["A"], [1.0, 2.0])


def test_unknown_campaign_raises(world):
    from wisper_transcribe.speaker_registry import relabel_campaign

    data, out = world
    with pytest.raises(KeyError):
        relabel_campaign("nope", output_dir=out, data_dir=data)


def test_path_like_stem_is_refused(world):
    """A path-like stem can't reach the relabel pass: the transcript
    registry's CHECK rejects it before it is stored."""
    import sqlite3

    data, out = world
    import wisper_transcribe.campaign_manager as cm

    campaigns = cm.load_campaigns(data)
    campaigns["game"].transcripts.append("../escape")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_save_campaigns(campaigns, data)