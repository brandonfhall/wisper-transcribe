"""Importing the JSON-era stores (migration v2) into wisper.db."""
from __future__ import annotations

import unicodedata

import numpy as np
import pytest

from wisper_transcribe import db
from wisper_transcribe.campaign_manager import load_campaigns
from wisper_transcribe.config import EMBEDDING_SPACE
from wisper_transcribe.speaker_manager import load_profiles

from ._legacy_store import write_campaigns, write_speakers


@pytest.fixture
def data_dir():
    d = db._data_dir(None)
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(db, "_cleaned", set())


def _vec(*xs):
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _report(data_dir) -> str:
    (backup,) = (data_dir / "backups").glob("pre-sqlite-v2-*")
    report = backup / "import-report.txt"
    return report.read_text(encoding="utf-8") if report.exists() else ""


def _output_md(stem: str) -> None:
    from wisper_transcribe.path_utils import get_output_dir
    (get_output_dir() / f"{stem}.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")


def test_clean_import(data_dir):
    write_speakers(
        data_dir,
        {"alice": {"display_name": "Alice", "role": "DM", "notes": "GM"}, "bob": {}},
        {"alice": _vec(1, 0, 0), "bob": _vec(0, 1, 0)},
    )
    write_campaigns(data_dir, {"game": {
        "display_name": "The Game", "created": "2026-02-03",
        "members": {"alice": {"role": "DM", "character": "", "discord_user_id": "111"},
                    "bob": {"role": "Player", "character": "Thorin"}},
        "transcripts": ["s01", "s02"],
    }})
    _output_md("s01")
    _output_md("s02")

    profiles = load_profiles()
    assert list(profiles) == ["alice", "bob"]
    assert profiles["alice"].display_name == "Alice"
    assert profiles["alice"].role == "DM"
    assert profiles["alice"].notes == "GM"
    np.testing.assert_array_almost_equal(profiles["alice"].embedding, _vec(1, 0, 0))
    assert profiles["alice"].embedding_space == EMBEDDING_SPACE

    game = load_campaigns()["game"]
    assert game.display_name == "The Game"
    assert game.created == "2026-02-03"
    assert game.members["alice"].discord_user_id == "111"
    assert game.members["bob"].character == "Thorin"
    assert game.transcripts == ["s01", "s02"]

    # Legacy files deleted after commit; copies kept in the backup dir.
    assert not (data_dir / "profiles" / "speakers.json").exists()
    assert not (data_dir / "campaigns" / "campaigns.json").exists()
    assert not list((data_dir / "profiles" / "embeddings").glob("*.npy"))
    (backup,) = (data_dir / "backups").glob("pre-sqlite-v2-*")
    assert (backup / "profiles" / "speakers.json").exists()
    assert (backup / "campaigns" / "campaigns.json").exists()
    assert (backup / "profiles" / "embeddings" / "alice.npy").exists()
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT backup_dir FROM migrations WHERE version = 2").fetchone()[0].startswith("backups/")


def test_reference_clips_stay_in_place(data_dir):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    clip = data_dir / "profiles" / "embeddings" / "alice.mp3"
    clip.write_bytes(b"mp3")
    load_profiles()
    assert clip.read_bytes() == b"mp3"


def test_rerun_imports_nothing_twice(data_dir):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    load_profiles()
    assert db.migrate() == []
    assert list(load_profiles()) == ["alice"]


def test_untagged_profile_keeps_old_model_marker(data_dir):
    write_speakers(data_dir, {"old": {"embedding_space": None}}, {"old": np.ones(512)})
    profile = load_profiles()["old"]
    assert profile.embedding_space == ""
    assert profile.embedding.shape == (512,)


def test_missing_embedding_imports_without_vector_and_reports(data_dir):
    write_speakers(data_dir, {"ghost": {}})  # no .npy written
    profile = load_profiles()["ghost"]
    assert profile.embedding is None
    assert "ghost" in _report(data_dir)


def test_embedding_file_outside_profiles_dir_is_ignored(data_dir, tmp_path):
    outside = tmp_path / "elsewhere.npy"
    np.save(str(outside), np.ones(4, dtype=np.float32))
    write_speakers(data_dir, {"alice": {"embedding_file": str(outside)}})
    assert load_profiles()["alice"].embedding is None
    assert outside.exists()


def test_member_without_profile_dropped_and_reported(data_dir):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    write_campaigns(data_dir, {"game": {"members": {"alice": {}, "nobody": {}}}})
    assert set(load_campaigns()["game"].members) == {"alice"}
    assert "nobody" in _report(data_dir)


def test_members_without_speakers_json(data_dir):
    write_campaigns(data_dir, {"game": {"members": {"alice": {}}, "transcripts": ["s01"]}})
    game = load_campaigns()["game"]
    assert game.members == {}
    assert game.transcripts == ["s01"]


def test_duplicate_discord_id_first_member_keeps_it(data_dir):
    write_speakers(data_dir, {"alice": {}, "bob": {}}, {"alice": _vec(1, 0), "bob": _vec(0, 1)})
    write_campaigns(data_dir, {"game": {"members": {
        "alice": {"discord_user_id": "42"}, "bob": {"discord_user_id": "42"}}}})
    members = load_campaigns()["game"].members
    assert members["alice"].discord_user_id == "42"
    assert members["bob"].discord_user_id is None
    assert "42" in _report(data_dir)


@pytest.mark.parametrize("raw", ["", "  ", "abc", "12x"])
def test_blank_or_non_numeric_discord_id_imports_as_unbound(data_dir, raw):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    write_campaigns(data_dir, {"game": {"members": {"alice": {"discord_user_id": raw}}}})
    assert load_campaigns()["game"].members["alice"].discord_user_id is None


def test_stem_in_two_campaigns_stays_in_first(data_dir):
    write_campaigns(data_dir, {
        "one": {"transcripts": ["s01", "s02"]},
        "two": {"transcripts": ["s02", "s03"]},
    })
    campaigns = load_campaigns()
    assert campaigns["one"].transcripts == ["s01", "s02"]
    assert campaigns["two"].transcripts == ["s03"]
    assert "s02" in _report(data_dir)


def test_stem_without_md_is_flagged_missing_and_keeps_order(data_dir):
    _output_md("s02")
    write_campaigns(data_dir, {"game": {"transcripts": ["s01", "s02", "s03"]}})
    assert load_campaigns()["game"].transcripts == ["s01", "s02", "s03"]
    with db.connection() as conn:
        missing = dict(conn.execute("SELECT stem, missing_since IS NOT NULL FROM transcripts"))
    assert missing == {"s01": 1, "s02": 0, "s03": 1}


def test_path_like_and_duplicate_stems_dropped(data_dir):
    write_campaigns(data_dir, {"game": {"transcripts": ["s01", "../x", "a\\b", "s01", ""]}})
    assert load_campaigns()["game"].transcripts == ["s01"]
    assert "../x" in _report(data_dir)


def test_stems_are_nfc_normalized_on_import(data_dir):
    nfd = unicodedata.normalize("NFD", "Café")
    write_campaigns(data_dir, {"game": {"transcripts": [nfd]}})
    assert load_campaigns()["game"].transcripts == [unicodedata.normalize("NFC", "Café")]


def test_bad_created_date_repaired_and_reported(data_dir):
    write_campaigns(data_dir, {"game": {"created": "last tuesday"}})
    assert len(load_campaigns()["game"].created) == 10
    assert "created" in _report(data_dir)


@pytest.mark.parametrize("which", ["speakers", "campaigns"])
def test_unreadable_store_rolls_back_and_names_file(data_dir, which):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    write_campaigns(data_dir, {"game": {}})
    target = (data_dir / "profiles" / "speakers.json" if which == "speakers"
              else data_dir / "campaigns" / "campaigns.json")
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(db.MigrationFailed, match=target.name) as exc:
        db.connect()
    assert "backups" in str(exc.value)
    with db.connection(migrate_schema=False, claim_runtime=False) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    # Nothing deleted: the user can fix the file and start again.
    assert (data_dir / "profiles" / "speakers.json").exists()
    assert (data_dir / "profiles" / "embeddings" / "alice.npy").exists()


def test_leftover_legacy_files_after_commit_are_removed_next_start(data_dir, monkeypatch):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    backup = data_dir / "profiles" / "speakers.json.keep"
    backup.write_bytes((data_dir / "profiles" / "speakers.json").read_bytes())
    load_profiles()
    # Simulate a crash between commit and delete: the file is back.
    backup.rename(data_dir / "profiles" / "speakers.json")
    monkeypatch.setattr(db, "_cleaned", set())  # a new process
    load_profiles()
    assert not (data_dir / "profiles" / "speakers.json").exists()
    assert list(load_profiles()) == ["alice"]


def test_fresh_install_has_no_backup(data_dir):
    load_profiles()
    assert not (data_dir / "backups").exists()


# ---------------------------------------------------------------------------
# v3: journaled_sessions frontmatter → journal_entries
# ---------------------------------------------------------------------------

def _write_journal(data_dir, slug, sessions, updated_at="2026-03-01T20:15:00"):
    import yaml

    path = data_dir / "campaigns" / slug / "journal.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump({"type": "campaign-journal", "campaign": slug,
                         "journaled_sessions": sessions, "updated_at": updated_at,
                         "provider": "ollama", "model": "m"}, sort_keys=False)
    path.write_text(f"---\n{fm}---\n\n## Story So Far\n\nThings.\n", encoding="utf-8")
    return path


def test_journaled_sessions_import_as_entries(data_dir):
    import hashlib

    from wisper_transcribe import journal

    write_campaigns(data_dir, {"game": {"transcripts": ["s1", "s2", "s3"]}})
    jpath = _write_journal(data_dir, "game", ["s1", "s2"])
    before = jpath.read_bytes()

    assert journal.journaled_stems("game") == ["s1", "s2"]
    assert jpath.read_bytes() == before  # the file itself isn't rewritten
    with db.connection() as conn:
        sha, stale = conn.execute(
            "SELECT journal_sha256, journal_stale_since FROM campaigns").fetchone()
        folded_at = conn.execute("SELECT DISTINCT folded_at FROM journal_entries").fetchall()
    assert sha == hashlib.sha256(before).hexdigest()
    assert stale is None
    assert len(folded_at) == 1 and folded_at[0][0].endswith("Z")
    assert journal.unjournalled_sessions("game") == []  # no summaries on disk


def test_journaled_session_not_in_campaign_dropped_and_reported(data_dir):
    from wisper_transcribe import journal

    write_campaigns(data_dir, {"game": {"transcripts": ["s1"]}, "other": {"transcripts": ["s9"]}})
    _write_journal(data_dir, "game", ["s1", "gone", "s9"])
    assert journal.journaled_stems("game") == ["s1"]
    report = _report(data_dir)
    assert "gone" in report and "s9" in report


def test_unreadable_journal_frontmatter_imports_no_entries(data_dir):
    from wisper_transcribe import journal

    write_campaigns(data_dir, {"game": {"transcripts": ["s1"]}})
    path = data_dir / "campaigns" / "game" / "journal.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n: : bad yaml [\n---\n\nbody\n", encoding="utf-8")
    assert journal.journaled_stems("game") == []
    assert "frontmatter" in _report(data_dir)


# ---------------------------------------------------------------------------
# v4: _diar.json speaker data → transcript_speakers + the audio file row
# ---------------------------------------------------------------------------

def _legacy_sidecar(out, stem, **fields):
    import json

    diar = {"input_path": "", "campaign": None,
            "diarization_segments": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}]}
    diar.update(fields)
    path = out / f"{stem}_diar.json"
    path.write_text(json.dumps(diar), encoding="utf-8")
    return path


def test_sidecar_speakers_import_and_file_is_slimmed(data_dir):
    import json

    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import read_sidecar

    out = get_output_dir()
    _output_md("s1")
    audio = out / "s1.wav"
    audio.write_bytes(b"a")
    path = _legacy_sidecar(out, "s1", input_path=str(audio),
                           speaker_map={"SPEAKER_00": "Alice"},
                           speaker_map_source={"SPEAKER_00": "manual"},
                           embedding_space=EMBEDDING_SPACE,
                           speaker_embeddings={"SPEAKER_00": [1.0, 0.0]})
    diar = read_sidecar(out / "s1.md")
    assert diar["speaker_map"] == {"SPEAKER_00": "Alice"}
    assert diar["speaker_map_source"] == {"SPEAKER_00": "manual"}
    assert diar["speaker_embeddings"] == {"SPEAKER_00": [1.0, 0.0]}
    assert diar["input_path"].endswith("s1.wav")
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"diarization_segments"}
    (backup,) = (data_dir / "backups").glob("pre-sqlite-v4-*")
    assert "speaker_map" in (backup / "output" / "s1_diar.json").read_text(encoding="utf-8")


def test_sidecar_without_provenance_derives_it(data_dir):
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import read_sidecar

    out = get_output_dir()
    _output_md("s1")
    _legacy_sidecar(out, "s1", speaker_map={"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 2"})
    assert read_sidecar(out / "s1.md")["speaker_map_source"] == {
        "SPEAKER_00": "manual", "SPEAKER_01": "auto"}


def test_sidecar_audio_outside_output_root_imports_as_none(data_dir, tmp_path):
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import read_sidecar

    out = get_output_dir()
    _output_md("s1")
    elsewhere = tmp_path / "upload.mp3"
    elsewhere.write_bytes(b"a")
    _legacy_sidecar(out, "s1", input_path=str(elsewhere), speaker_map={"SPEAKER_00": "A"})
    assert read_sidecar(out / "s1.md")["input_path"] == ""
    assert "s1" in _v4_report(data_dir)


@pytest.mark.parametrize("old_path", [
    "/Users/someone/Library/Application Support/wisper-transcribe/output/s1.mp3",
    r"C:\Users\someone\AppData\Local\wisper-transcribe\output\s1.mp3",
])
def test_sidecar_audio_from_a_moved_data_folder_links_by_name(data_dir, old_path):
    # A data folder copied into Docker or to another machine keeps the old
    # absolute path; the audio next to the transcript is still found.
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import read_sidecar

    out = get_output_dir()
    _output_md("s1")
    (out / "s1.mp3").write_bytes(b"a")
    _legacy_sidecar(out, "s1", input_path=old_path, speaker_map={"SPEAKER_00": "A"})
    assert read_sidecar(out / "s1.md")["input_path"] == str(out / "s1.mp3")
    assert "s1" not in _v4_report(data_dir)


def test_sidecar_campaign_key_associates_unassigned_transcript(data_dir):
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    write_campaigns(data_dir, {"game": {"transcripts": []}})
    _output_md("s1")
    _legacy_sidecar(out, "s1", campaign="game", speaker_map={"SPEAKER_00": "A"})
    assert load_campaigns()["game"].transcripts == ["s1"]


def test_orphan_and_unreadable_sidecars_left_alone(data_dir):
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    orphan = _legacy_sidecar(out, "gone", speaker_map={"SPEAKER_00": "A"})
    bad = out / "bad_diar.json"
    bad.write_text("{nope", encoding="utf-8")
    _output_md("bad")
    before = orphan.read_text(encoding="utf-8")
    db.connect().close()
    assert orphan.read_text(encoding="utf-8") == before
    assert bad.read_text(encoding="utf-8") == "{nope"
    assert "bad_diar.json" in _v4_report(data_dir)


def _v4_report(data_dir) -> str:
    reports = list((data_dir / "backups").glob("pre-sqlite-*/import-report.txt"))
    return "\n".join(r.read_text(encoding="utf-8") for r in reports)


# ---------------------------------------------------------------------------
# v5: recordings.json + metadata.json → recordings tables
# ---------------------------------------------------------------------------

from ._legacy_store import write_recording  # noqa: E402

RID = "11111111-2222-4333-8444-555555555555"
RID2 = "66666666-7777-4888-8999-000000000000"


def _recordings():
    from wisper_transcribe.recording_manager import load_recordings
    return load_recordings()


def test_recording_import_full(data_dir):
    from wisper_transcribe.path_utils import get_output_dir

    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    write_campaigns(data_dir, {"game": {}})
    md = get_output_dir() / f"{RID}.md"
    md.write_text("x", encoding="utf-8")
    rec_dir = write_recording(
        data_dir, RID, campaign_slug="game", status="transcribed", name="Session 3",
        transcript_path=str(md),
        discord_speakers={"111": "alice", "222": ""}, unbound_speakers=["222", "333"],
        segment_manifest=[{"index": 0, "stream": "mixed", "started_at": "2026-03-01T19:00:00.000000+0000",
                           "duration_s": 60.0, "path": str(data_dir / "recordings" / RID / "combined" / "0000.wav"),
                           "finalized": True}],
        markers=[{"timestamp": "2026-03-01T19:05:00.000000+0000", "elapsed_s": 300.0}],
        rejoin_log=[{"timestamp": "2026-03-01T19:10:00.000000+0000", "close_code": 4006, "attempt_number": 1}],
        job_id="stale-job",
    )
    r = _recordings()[RID]
    assert r.status == "transcribed" and r.transcript_path == md
    assert r.name == "Session 3"
    # The capture-time campaign is stored; a transcribed recording shows its
    # transcript's campaign, and the imported transcript is in none.
    with db.connection(data_dir) as conn:
        stored = conn.execute("SELECT c.slug FROM recordings r JOIN campaigns c "
                              "ON c.id = r.campaign_id WHERE r.id = ?", (RID,)).fetchone()[0]
    assert stored == "game" and r.campaign_slug is None
    assert r.discord_speakers == {"111": "alice", "222": "", "333": ""}
    assert r.unbound_speakers == ["222", "333"]
    assert [s.index for s in r.segment_manifest] == [0]
    assert r.markers[0].elapsed_s == 300.0
    assert r.rejoin_log[0].close_code == 4006
    assert r.job_id is None                       # derived, never imported
    assert not (rec_dir / "metadata.json").exists()
    assert not (data_dir / "recordings" / "recordings.json").exists()
    (backup,) = (data_dir / "backups").glob("pre-sqlite-v5-*")
    assert (backup / "recordings" / RID / "metadata.json").exists()


def test_recording_status_mapping(data_dir):
    write_recording(data_dir, RID, status="transcribing")
    write_recording(data_dir, RID2, status="recording", ended_at=None)
    recs = _recordings()
    assert recs[RID].status == "completed"        # no transcript, no active job
    assert recs[RID2].status == "failed" and recs[RID2].ended_at is not None
    assert "marked failed" in _v4_report(data_dir)


def test_recording_dirty_fields_repaired_and_reported(data_dir, tmp_path):
    write_speakers(data_dir, {"alice": {}}, {"alice": _vec(1, 0)})
    write_recording(data_dir, RID, campaign_slug="gone", discord_speakers={"111": "deleted_profile"},
                    transcript_path=str(tmp_path / "elsewhere" / "x.md"))
    write_recording(data_dir, RID2, source="local", devices={"mic": "USB Mic", "bogus": "x"},
                    discord_speakers={"111": ""},
                    segment_manifest=[{"index": 0, "stream": "444", "started_at": "2026-03-01T19:00:00.000000+0000",
                                       "duration_s": 1.0, "path": "/tmp/0000.wav", "finalized": True}])
    recs = _recordings()
    assert recs[RID].campaign_slug is None
    assert recs[RID].unbound_speakers == ["111"]
    assert recs[RID].transcript_path is None
    assert recs[RID2].devices == {"mic": "USB Mic"}
    assert recs[RID2].discord_speakers == {} and recs[RID2].segment_manifest == []
    report = _v4_report(data_dir)
    for fragment in ("no longer exists", "missing profile", "outside the transcripts folder",
                     "local session listed Discord speakers", "off the standard layout"):
        assert fragment in report, fragment


def test_recording_shared_transcript_goes_to_first(data_dir):
    from wisper_transcribe.path_utils import get_output_dir

    md = get_output_dir() / "shared.md"
    md.write_text("x", encoding="utf-8")
    write_recording(data_dir, RID, status="transcribed", transcript_path=str(md))
    write_recording(data_dir, RID2, status="transcribed", transcript_path=str(md))
    recs = _recordings()
    assert [recs[i].status for i in (RID, RID2)] == ["transcribed", "completed"]
    assert "already belongs" in _v4_report(data_dir)


def test_recording_bad_id_and_unreadable_metadata_skipped(data_dir):
    write_recording(data_dir, "not-a-uuid")
    write_recording(data_dir, RID)
    (data_dir / "recordings" / RID / "metadata.json").write_text("{bad", encoding="utf-8")
    assert _recordings() == {}
    report = _v4_report(data_dir)
    assert "not-a-uuid" in report and RID in report


@pytest.mark.parametrize("raw, expected", [
    ("2026-03-01T20:15:30.123456+00:00", "2026-03-01T20:15:30.123456Z"),   # the JSON-era format
    ("2026-03-01T21:15:30.5+01:00", "2026-03-01T20:15:30.500000Z"),        # other offset, short fraction
    ("2026-03-01T20:15:30", "2026-03-01T20:15:30.000000Z"),                # naive: taken as UTC
    ("not a time", None), ("", None), (None, None), (1700000000, None),
])
def test_legacy_recording_times_normalize_or_drop(raw, expected):
    from wisper_transcribe.legacy_import import _legacy_time
    assert _legacy_time(raw) == expected
