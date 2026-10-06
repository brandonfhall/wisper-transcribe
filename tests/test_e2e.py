"""End-to-end flows through the web app against a real temp database.

Only the ML and LLM boundaries are mocked (Whisper, pyannote, embedding
extraction, the LLM client, ffmpeg checks). Everything else is real: the job
queue, pipeline, aligner, formatter, transcript store, campaign and journal
code, the search index, and SQLite. Each step checks database rows, files on
disk, and the search index.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from wisper_transcribe import db, file_registry, search_index
from wisper_transcribe.models import DiarizationSegment, TranscriptionSegment
from wisper_transcribe.path_utils import get_output_dir

from ._legacy_store import write_campaigns, write_speakers
from ._seed import seed_recording, unit

SEGMENTS = [
    TranscriptionSegment(start=0.0, end=4.0, text="Welcome back, the party stands before Castle Ravenloft."),
    TranscriptionSegment(start=5.0, end=9.0, text="I knock on the gate and call for Strahd."),
]
DIARIZATION = [
    DiarizationSegment(start=0.0, end=4.5, speaker="SPEAKER_00"),
    DiarizationSegment(start=4.5, end=9.5, speaker="SPEAKER_01"),
]
SUMMARY = {"summary": "The party reached Castle Ravenloft and summoned Strahd.",
           "session_title": "At the Gate", "loot": [], "npcs": [{"name": "Strahd"}]}


def _convert_to_wav(path, out_path=None):
    """convert_to_wav stand-in: writes a tiny WAV where asked, else passes through."""
    import wave

    if out_path is None:
        return Path(path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    return out_path


def _encode_flac(src, dst):
    Path(dst).write_bytes(b"fLaC-fake")


def _embedding(*args, **kwargs):
    label = kwargs.get("speaker_label") or args[2]
    return unit(np.eye(256)[0 if label == "SPEAKER_00" else 1])


@pytest.fixture
def ml():
    """Mock only the ML/LLM boundaries."""
    llm = MagicMock()
    llm.complete_json.return_value = SUMMARY
    llm.complete.return_value = "# Journal\n\nSession 1: the party reached the castle gate."
    llm.provider, llm.model = "fake", "fake-model"
    with ExitStack() as stack:
        for target, kw in [
            ("wisper_transcribe.pipeline.check_ffmpeg", {}),
            ("wisper_transcribe.pipeline.validate_audio", {}),
            ("wisper_transcribe.pipeline.convert_to_wav", {"side_effect": lambda p, *a, **k: Path(p)}),
            ("wisper_transcribe.audio_utils.convert_to_wav", {"side_effect": _convert_to_wav}),
            ("wisper_transcribe.audio_utils.encode_flac", {"side_effect": _encode_flac}),
            ("wisper_transcribe.pipeline.get_duration", {"return_value": 10.0}),
            ("wisper_transcribe.pipeline.transcribe", {"return_value": SEGMENTS}),
            ("wisper_transcribe.pipeline.get_hf_token", {"return_value": "hf_fake"}),
            ("wisper_transcribe.diarizer.diarize", {"return_value": DIARIZATION}),
            ("wisper_transcribe.speaker_manager.extract_embedding", {"side_effect": _embedding}),
            ("wisper_transcribe.llm.get_client", {"return_value": llm}),
        ]:
            stack.enter_context(patch(target, **kw))
        yield llm


@pytest.fixture
def client():
    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _wait(client, job_id: str, timeout: float = 20.0):
    queue = client.app.state.job_queue
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job is not None and job.status in ("completed", "failed"):
            assert job.status == "completed", (job.error, job.log_lines[-10:])
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _job_from(resp) -> str:
    assert resp.status_code == 303, resp.text[:300]
    return resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def _row(sql: str, *params):
    with db.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return tuple(row) if row is not None else None


def _count(sql: str, *params) -> int:
    return _row(sql, *params)[0]


def _upload(client, stem: str, campaign: str = "") -> str:
    data = {"campaign": campaign} if campaign else {}
    resp = client.post("/transcribe", files={"file": (f"{stem}.mp3", b"ID3fake-audio", "audio/mpeg")},
                       data=data, follow_redirects=False)
    return _job_from(resp)


# ---------------------------------------------------------------------------
# 1. Upload → transcribe → campaign → summarize → journal → rename → search → delete
# ---------------------------------------------------------------------------

def test_full_session_lifecycle(client, ml):
    out = get_output_dir()
    assert client.post("/campaigns", data={"display_name": "Curse of Strahd"},
                       follow_redirects=False).status_code == 303
    folder = out / "Curse of Strahd"

    # Transcribe into the campaign.
    _wait(client, _upload(client, "Session 1", "curse-of-strahd"))
    md = folder / "Session 1.md"
    assert md.is_file() and "Castle Ravenloft" in md.read_text(encoding="utf-8")
    tid = _row("SELECT id FROM transcripts WHERE stem = 'Session 1'")[0]
    audio = file_registry.file_for(file_registry.Owner("transcript", tid), "audio")
    assert audio is not None and audio.path == folder / "Session 1.flac" and audio.path.is_file()
    assert _count("SELECT count(*) FROM transcripts t JOIN campaigns c ON c.id = t.campaign_id "
                  "WHERE t.id = ? AND c.slug = 'curse-of-strahd'", tid) == 1
    assert _count("SELECT count(*) FROM transcript_speakers WHERE transcript_id = ?", tid) == 2
    assert (folder / "Session 1_diar.json").is_file()
    assert [g.stem for g in search_index.search("ravenloft").groups] == ["Session 1"]

    # Summarize.
    _wait(client, _job_from(client.post(f"/transcripts/{tid}/summarize", follow_redirects=False)))
    assert (folder / "Session 1.summary.md").is_file()
    hits = search_index.search("summoned", kind="summary").groups
    assert [g.stem for g in hits] == ["Session 1"]

    # Fold into the journal.
    _wait(client, _job_from(client.post("/campaigns/curse-of-strahd/journal", data={"mode": "next"},
                                        follow_redirects=False)))
    assert _count("SELECT count(*) FROM journal_entries WHERE transcript_id = ?", tid) == 1
    assert _row("SELECT journal_sha256 FROM campaigns WHERE slug = 'curse-of-strahd'")[0]

    # Rename a speaker in the wizard.
    resp = client.post(f"/transcripts/{tid}/enroll", data={"speaker_SPEAKER_01": "Ezmerelda"},
                       follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    if "/transcribe/jobs/" in location:
        _wait(client, location.rsplit("/", 1)[-1])
    assert "**Ezmerelda**" in md.read_text(encoding="utf-8")
    assert _row("SELECT display_name, source FROM transcript_speakers "
                "WHERE transcript_id = ? AND label = 'SPEAKER_01'", tid) == ("Ezmerelda", "manual")
    assert "Ezmerelda" in search_index.speakers()
    page = client.get("/search", params={"q": "strahd", "speaker": "Ezmerelda"}).text
    assert 'data-testid="search-hit"' in page and "#b-1" in page

    # Delete: file, row, links, index, companions; the journal goes stale.
    assert client.post(f"/transcripts/{tid}/delete", follow_redirects=False).status_code == 303
    assert not md.exists() and not (folder / "Session 1.summary.md").exists()
    assert not (folder / "Session 1_diar.json").exists() and not audio.path.exists()
    for table in ("transcripts", "journal_entries", "transcript_speakers",
                  "search_index_state", "search_blocks"):
        col = "id" if table == "transcripts" else "transcript_id"
        assert _count(f"SELECT count(*) FROM {table} WHERE {col} = ?", tid) == 0, table
    assert search_index.search("ravenloft").groups == []
    assert _row("SELECT journal_stale_since FROM campaigns WHERE slug = 'curse-of-strahd'")[0]


# ---------------------------------------------------------------------------
# 2. A JSON-era data dir → import on first start → the same flow
# ---------------------------------------------------------------------------

def test_legacy_install_imports_then_works(ml, tmp_path, monkeypatch):
    data = tmp_path / "legacy-data"
    monkeypatch.setenv("WISPER_DATA_DIR", str(data))
    (data / "output").mkdir(parents=True)
    write_speakers(data, {"alice": {"display_name": "Alice"}}, embeddings={"alice": unit(np.eye(256)[0])})
    write_campaigns(data, {"cos": {"display_name": "Curse", "members": {"alice": {"role": "DM"}}}})
    (data / "config.toml").write_text("", encoding="utf-8")

    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as client:  # startup migrates and imports
        assert not (data / "profiles" / "speakers.json").exists()
        assert list((data / "backups").glob("pre-sqlite-v2-*/profiles/speakers.json"))
        assert _count("SELECT count(*) FROM campaign_members m JOIN profiles p ON p.id = m.profile_id "
                      "WHERE p.key = 'alice'") == 1

        _wait(client, _upload(client, "Session 2", "cos"))
        md = get_output_dir() / "Curse" / "Session 2.md"
        # The imported profile matched SPEAKER_00 by its embedding.
        assert "**Alice**" in md.read_text(encoding="utf-8")
        assert [g.stem for g in search_index.search("gate", speaker="Alice").groups] == []
        assert [g.stem for g in search_index.search("ravenloft", speaker="Alice").groups] == ["Session 2"]
        tid = _row("SELECT id FROM transcripts WHERE stem = 'Session 2'")[0]
        assert client.post(f"/transcripts/{tid}/delete", follow_redirects=False).status_code == 303
        assert _count("SELECT count(*) FROM transcripts") == 0


# ---------------------------------------------------------------------------
# 3. Discord recording → transcribe → delete transcript → transcribable again
# ---------------------------------------------------------------------------

def test_recording_transcript_delete_reopens_recording(client, ml):
    from wisper_transcribe.recording_manager import load_recording

    rec = seed_recording(status="completed")
    assert rec.status == "completed"
    resp = client.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)
    assert resp.status_code == 303
    # Job history is written at submit, with the recording as its subject.
    (job_id,) = _row("SELECT id FROM jobs WHERE recording_id = ? ORDER BY created_at DESC", rec.id)
    _wait(client, job_id)

    done = load_recording(rec.id)
    assert done.status == "transcribed" and done.transcript_path is not None
    stem = Path(done.transcript_path).stem
    assert search_index.search("ravenloft").groups[0].stem == stem
    tid = _row("SELECT id FROM transcripts WHERE stem = ?", stem)[0]

    assert client.post(f"/transcripts/{tid}/delete", follow_redirects=False).status_code == 303
    again = load_recording(rec.id)
    assert again.status == "completed" and again.transcript_path is None
    assert "Transcribe" in client.get(f"/recordings/{rec.id}").text


# ---------------------------------------------------------------------------
# Re-transcribe from the saved audio keeps the campaign place; the journal goes stale
# ---------------------------------------------------------------------------

def test_retranscribe_keeps_campaign_position_and_marks_journal_stale(client, ml):
    from wisper_transcribe.campaign_manager import get_transcripts_for_campaign

    out = get_output_dir()
    assert client.post("/campaigns", data={"display_name": "Curse of Strahd"},
                       follow_redirects=False).status_code == 303
    folder = out / "Curse of Strahd"
    for stem in ("Session 1", "Session 2", "Session 3"):
        _wait(client, _upload(client, stem, "curse-of-strahd"))
    s1_id = _row("SELECT id FROM transcripts WHERE stem = 'Session 1'")[0]
    _wait(client, _job_from(client.post(f"/transcripts/{s1_id}/summarize", follow_redirects=False)))
    _wait(client, _job_from(client.post("/campaigns/curse-of-strahd/journal", data={"mode": "next"},
                                        follow_redirects=False)))
    tid, stem = _row("SELECT t.id, t.stem FROM journal_entries je "
                     "JOIN transcripts t ON t.id = je.transcript_id")
    assert _row("SELECT journal_stale_since FROM campaigns WHERE slug = 'curse-of-strahd'")[0] is None
    flac = folder / f"{stem}.flac"
    order = get_transcripts_for_campaign("curse-of-strahd")

    _wait(client, _job_from(client.post(f"/transcripts/{tid}/retranscribe",
                                        follow_redirects=False)))

    assert get_transcripts_for_campaign("curse-of-strahd") == order
    assert _row("SELECT id FROM transcripts WHERE stem = ?", stem)[0] == tid
    assert flac.is_file() and sorted(p.name for p in folder.glob("*.flac")) == [
        "Session 1.flac", "Session 2.flac", "Session 3.flac"]
    assert file_registry.file_for(file_registry.Owner("transcript", tid), "audio").path == flac
    assert _row("SELECT journal_stale_since FROM campaigns WHERE slug = 'curse-of-strahd'")[0]


# ---------------------------------------------------------------------------
# Campaign folders: upload, move, rename, rename campaign, organize, delete
# ---------------------------------------------------------------------------

def test_campaign_folder_lifecycle(client, ml):
    import os

    from wisper_transcribe import storage_trim, transcript_store

    out = get_output_dir()
    for name in ("Curse of Strahd", "Tomb of Annihilation"):
        assert client.post("/campaigns", data={"display_name": name},
                           follow_redirects=False).status_code == 303
    strahd, tomb = out / "Curse of Strahd", out / "Tomb of Annihilation"

    def names(folder):
        return sorted(p.name for p in folder.iterdir())

    # Upload into a campaign: the session and its files land in its folder.
    _wait(client, _upload(client, "Session 1", "curse-of-strahd"))
    tid = _row("SELECT id FROM transcripts WHERE stem = 'Session 1'")[0]
    assert "Session 1.md" in names(strahd) and "Session 1.flac" in names(strahd)

    # Move to another campaign: every file follows.
    resp = client.post(f"/transcripts/{tid}/campaign", data={"campaign": "tomb-of-annihilation"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert not [n for n in names(strahd) if n.startswith("Session 1")]
    assert {"Session 1.md", "Session 1.flac", "Session 1_diar.json"} <= set(names(tomb))

    # Rename the session: the .md and its companions are renamed together.
    resp = client.post(f"/transcripts/{tid}/rename", data={"new_name": "Session One"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert not [n for n in names(tomb) if n.startswith("Session 1")]
    assert {"Session One.md", "Session One.flac", "Session One_diar.json"} <= set(names(tomb))

    # Rename the campaign: its folder moves and the session is found inside it.
    resp = client.post("/campaigns/tomb-of-annihilation/rename",
                       data={"display_name": "Tomb Renamed"}, follow_redirects=False)
    assert resp.headers["location"] == "/campaigns/tomb-renamed"
    renamed = out / "Tomb Renamed"
    assert not tomb.exists() and "Session One.md" in names(renamed)
    assert transcript_store.locate(tid).dir == renamed
    assert client.get(f"/transcripts/{tid}").status_code == 200

    # A shell mv of the whole session to the root is followed as a move out of
    # the campaign, with every companion.
    for path in list(renamed.iterdir()):
        if path.name.startswith("Session One"):
            os.replace(path, out / path.name)
    transcript_store.reconcile(out)
    loc = transcript_store.locate(tid)
    assert loc.campaign_id is None and loc.dir == out and not loc.misplaced

    # Assigned to the campaign with its files still in the root (as an upgrade
    # leaves sessions): misplaced, and storage trim moves the files home.
    from wisper_transcribe.campaign_manager import _assign

    cid = _row("SELECT id FROM campaigns WHERE slug = 'tomb-renamed'")[0]
    with db.transaction() as conn:
        _assign(conn, tid, cid, 0)
    assert transcript_store.locate(tid).misplaced
    report = storage_trim.apply()
    assert report.organized == ["Session One"]
    assert not transcript_store.locate(tid).misplaced
    assert "Session One.md" in names(renamed)

    # Delete the campaign, keeping the files: the session moves to the root.
    resp = client.post("/campaigns/tomb-renamed/delete", data={"mode": "keep"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert (out / "Session One.md").is_file() and (out / "Session One.flac").is_file()
    assert not renamed.exists()
    loc = transcript_store.locate(tid)
    assert loc.campaign_id is None and loc.dir == out and not loc.misplaced
