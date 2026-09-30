"""Every schema constraint, cascade, and trigger, exercised directly in SQL.

Each violation statement breaks exactly one rule against a valid baseline,
on a connection from db.connect() (foreign_keys=ON), and must raise
IntegrityError. Behaviour through the managers is tested in their own files;
this file pins the database itself.
"""
from __future__ import annotations

import sqlite3

import pytest

from wisper_transcribe import db

R = "11111111-1111-4111-8111-111111111111"   # Discord recording
L = "22222222-2222-4222-8222-222222222222"   # local recording
N = "33333333-3333-4333-8333-333333333333"   # unused id for inserts
J = "44444444-4444-4444-8444-444444444444"   # job


@pytest.fixture
def conn():
    c = db.connect()
    c.executescript(f"""
        INSERT INTO profiles (id, key, display_name, enrolled_date, enrollment_source)
          VALUES (1, 'alice', 'Alice', '2026-01-01', 's.mp3'), (2, 'bob', 'Bob', '2026-01-01', 's.mp3');
        INSERT INTO campaigns (id, slug, display_name, created_at) VALUES (1, 'game', 'Game', 'now'),
                                                                        (2, 'other', 'Other', 'now');
        INSERT INTO campaign_members (campaign_id, profile_id, discord_user_id) VALUES (1, 1, '111');
        INSERT INTO transcripts (id, stem, created_at) VALUES (1, 's1', 'now'), (2, 's2', 'now'), (3, 's3', 'now');
        INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) VALUES (1, 1, 0), (2, 1, 1);
        INSERT INTO journal_entries (transcript_id, campaign_id, folded_at) VALUES (1, 1, 'now');
        INSERT INTO transcript_speakers (transcript_id, label, display_name, source)
          VALUES (1, 'SPEAKER_00', 'Alice', 'auto');
        INSERT INTO recordings (id, source, capture_status, started_at, campaign_id, transcript_id)
          VALUES ('{R}', 'discord', 'completed', 'now', 1, 3),
                 ('{L}', 'local', 'recording', 'now', NULL, NULL);
        INSERT INTO recording_discord (recording_id, guild_id, voice_channel_id) VALUES ('{R}', 'g', 'v');
        INSERT INTO recording_devices (recording_id, role, device_name) VALUES ('{L}', 'mic', 'Mic');
        INSERT INTO recording_speakers (recording_id, discord_user_id, profile_id) VALUES ('{R}', '111', 1);
        INSERT INTO recording_segments (recording_id, idx, started_at, duration_s, finalized)
          VALUES ('{R}', 0, 'now', 1.0, 1);
        INSERT INTO recording_markers (recording_id, marked_at) VALUES ('{R}', 'now');
        INSERT INTO recording_rejoins (recording_id, attempted_at, close_code, attempt_number)
          VALUES ('{R}', 'now', 4000, 1);
        INSERT INTO jobs (id, type, status, created_at, transcript_id, campaign_id, recording_id)
          VALUES ('{J}', 'summarize', 'pending', 'now', 1, 1, '{R}');
        INSERT INTO search_index_state VALUES (1, 'transcript', 1, 10);
        INSERT INTO search_blocks (id, transcript_id, kind, block_idx, speaker, start_s)
          VALUES (1, 1, 'transcript', 0, 'Alice', 0.0);
        INSERT INTO search_fts (rowid, text) VALUES (1, 'the party meets strahd');
    """)
    yield c
    c.close()


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


VIOLATIONS = {
    # runtime_leases
    "lease runtime enum": "INSERT INTO runtime_leases VALUES ('laptop', 'h', 0, 'now')",
    "lease host crosses vm": "INSERT INTO runtime_leases VALUES ('host', 'h', 1, 'now')",
    # profiles
    "profile key unique": "INSERT INTO profiles (key, display_name, enrolled_date, enrollment_source) "
                          "VALUES ('alice', 'A', 'd', 's')",
    "profile empty name": "UPDATE profiles SET display_name = '' WHERE id = 1",
    "profile embedding without space": "UPDATE profiles SET embedding = x'00000000' WHERE id = 1",
    "profile space without embedding": "UPDATE profiles SET embedding_space = 'x' WHERE id = 1",
    "profile embedding not float32": "UPDATE profiles SET embedding = x'000000', embedding_space = 'x' WHERE id = 1",
    "strict type": "UPDATE campaign_transcripts SET position = 'first' WHERE transcript_id = 1",
    # campaigns
    "campaign slug unique": "INSERT INTO campaigns (slug, display_name, created_at) VALUES ('game', 'G', 'now')",
    "campaign empty name": "UPDATE campaigns SET display_name = '' WHERE id = 1",
    "campaign sha length": "UPDATE campaigns SET journal_sha256 = 'abc' WHERE id = 1",
    # campaign_members
    "member discord digits": "UPDATE campaign_members SET discord_user_id = '12a' WHERE profile_id = 1",
    "member discord empty": "UPDATE campaign_members SET discord_user_id = '' WHERE profile_id = 1",
    "member one account per campaign": "INSERT INTO campaign_members (campaign_id, profile_id, discord_user_id) "
                                       "VALUES (1, 2, '111')",
    "member unknown profile": "INSERT INTO campaign_members (campaign_id, profile_id) VALUES (1, 99)",
    "member twice": "INSERT INTO campaign_members (campaign_id, profile_id) VALUES (1, 1)",
    # transcripts
    "stem unique": "INSERT INTO transcripts (stem, created_at) VALUES ('s1', 'now')",
    "stem empty": "INSERT INTO transcripts (stem, created_at) VALUES ('', 'now')",
    "stem slash": "INSERT INTO transcripts (stem, created_at) VALUES ('a/b', 'now')",
    "stem backslash": "INSERT INTO transcripts (stem, created_at) VALUES ('a\\b', 'now')",
    "audio path absolute": "UPDATE transcripts SET audio_rel_path = '/etc/x.mp3' WHERE id = 1",
    "audio path backslash": "UPDATE transcripts SET audio_rel_path = 'a\\x.mp3' WHERE id = 1",
    "audio path dotdot": "UPDATE transcripts SET audio_rel_path = 'a/../../x.mp3' WHERE id = 1",
    # campaign_transcripts
    "one campaign per transcript": "INSERT INTO campaign_transcripts VALUES (1, 2, 0)",
    "position unique": "INSERT INTO campaign_transcripts VALUES (3, 1, 0)",
    "position negative": "INSERT INTO campaign_transcripts VALUES (3, 1, -1)",
    # journal_entries
    "journal entry outside its campaign": "INSERT INTO journal_entries VALUES (2, 2, 'now')",
    "journal entry unassigned transcript": "INSERT INTO journal_entries VALUES (3, 1, 'now')",
    "journaled transcript can't move without dropping entry":
        "UPDATE campaign_transcripts SET campaign_id = 2 WHERE transcript_id = 1",
    # transcript_speakers
    "speaker source enum": "INSERT INTO transcript_speakers (transcript_id, label, display_name, source) "
                           "VALUES (1, 'SPEAKER_01', 'B', 'guess')",
    "speaker label unique": "INSERT INTO transcript_speakers (transcript_id, label, display_name, source) "
                            "VALUES (1, 'SPEAKER_00', 'B', 'auto')",
    "speaker embedding pair": "UPDATE transcript_speakers SET embedding = x'00000000' WHERE transcript_id = 1",
    # recordings
    "recording id length": "INSERT INTO recordings (id, source, capture_status, started_at) "
                           "VALUES ('short', 'local', 'completed', 'now')",
    "recording source enum": f"INSERT INTO recordings (id, source, capture_status, started_at) "
                             f"VALUES ('{N}', 'zoom', 'completed', 'now')",
    "recording status enum (derived states not stored)":
        "UPDATE recordings SET capture_status = 'transcribed' WHERE source = 'local'",
    "active recording has no end": f"UPDATE recordings SET ended_at = 'now' WHERE id = '{L}'",
    "recovered only when completed": f"UPDATE recordings SET recovered_at = 'now' WHERE id = '{L}'",
    "one recording per transcript": f"UPDATE recordings SET transcript_id = 3 WHERE id = '{L}'",
    # subtypes
    "discord row on a local recording": f"INSERT INTO recording_discord (recording_id, guild_id, voice_channel_id) "
                                        f"VALUES ('{L}', 'g', 'v')",
    "device row on a discord recording": f"INSERT INTO recording_devices (recording_id, role, device_name) "
                                         f"VALUES ('{R}', 'mic', 'Mic')",
    "device role enum": f"INSERT INTO recording_devices (recording_id, role, device_name) VALUES ('{L}', 'aux', 'X')",
    "discord subtype source pinned": f"UPDATE recording_discord SET source = 'local' WHERE recording_id = '{R}'",
    "speaker on a local recording": f"INSERT INTO recording_speakers (recording_id, discord_user_id) "
                                    f"VALUES ('{L}', '222')",
    "recording speaker digits": f"INSERT INTO recording_speakers (recording_id, discord_user_id) VALUES ('{R}', 'x1')",
    "rejoin on a local recording": f"INSERT INTO recording_rejoins VALUES ('{L}', 'now', 4000, 1)",
    "rejoin attempt >= 1": f"INSERT INTO recording_rejoins VALUES ('{R}', 'later', 4000, 0)",
    # segments / markers
    "segment idx >= 0": f"INSERT INTO recording_segments VALUES ('{R}', -1, 'now', 1.0, 1)",
    "segment duration >= 0": f"INSERT INTO recording_segments VALUES ('{R}', 1, 'now', -1.0, 1)",
    "segment finalized boolean": f"INSERT INTO recording_segments VALUES ('{R}', 1, 'now', 1.0, 2)",
    "marker unique time": f"INSERT INTO recording_markers VALUES ('{R}', 'now')",
    # jobs
    "job type enum": f"INSERT INTO jobs (id, type, status, created_at) VALUES ('{N}', 'mine', 'pending', 'now')",
    "job status enum": f"INSERT INTO jobs (id, type, status, created_at) VALUES ('{N}', 'refine', 'queued', 'now')",
    "pending job not started": f"UPDATE jobs SET started_at = 'now' WHERE id = '{J}'",
    "running job started": f"UPDATE jobs SET status = 'running' WHERE id = '{J}'",
    "finished job has finish time": f"UPDATE jobs SET status = 'completed', started_at = 'now' WHERE id = '{J}'",
    "failed job has error code": f"UPDATE jobs SET status = 'failed', started_at = 'now', finished_at = 'now' "
                                 f"WHERE id = '{J}'",
    "error code only when failed": f"UPDATE jobs SET error_code = 'x' WHERE id = '{J}'",
    "job params json object": f"UPDATE jobs SET params_json = '[1]' WHERE id = '{J}'",
    # search
    "search kind enum": "INSERT INTO search_index_state VALUES (2, 'journal', 1, 1)",
    "search size >= 0": "INSERT INTO search_index_state VALUES (2, 'transcript', 1, -1)",
    "block needs its state row": "INSERT INTO search_blocks (transcript_id, kind, block_idx) VALUES (2, 'transcript', 0)",
}


@pytest.mark.parametrize("sql", VIOLATIONS.values(), ids=VIOLATIONS.keys())
def test_violation_is_rejected(conn, sql):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_baseline_satisfies_every_rule(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_summary_blocks_have_no_speaker_or_time(conn):
    conn.execute("INSERT INTO search_index_state VALUES (1, 'summary', 1, 1)")
    for extra in ("speaker", "start_s"):
        value = "'Alice'" if extra == "speaker" else "1.0"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO search_blocks (transcript_id, kind, block_idx, {extra}) "
                         f"VALUES (1, 'summary', 0, {value})")


# ---------------------------------------------------------------------------
# Cascades and triggers
# ---------------------------------------------------------------------------

def test_transcript_delete_cascades_and_marks_journal_stale(conn):
    conn.execute("DELETE FROM transcripts WHERE id = 1")
    for table in ("campaign_transcripts", "journal_entries", "transcript_speakers",
                  "search_index_state", "search_blocks"):
        assert _one(conn, f"SELECT count(*) FROM {table} WHERE transcript_id = 1") == 0, table
    assert _one(conn, "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'strahd'") == 0
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None
    assert _one(conn, f"SELECT transcript_id FROM jobs WHERE id = '{J}'") is None  # history kept


def test_deleting_recordings_transcript_makes_it_transcribable_again(conn):
    conn.execute("DELETE FROM transcripts WHERE id = 3")
    assert _one(conn, f"SELECT transcript_id FROM recordings WHERE id = '{R}'") is None


def test_unassigning_journaled_transcript_drops_entry_and_marks_stale(conn):
    conn.execute("DELETE FROM campaign_transcripts WHERE transcript_id = 1")
    assert _one(conn, "SELECT count(*) FROM journal_entries") == 0
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None


def test_campaign_delete(conn):
    conn.execute("DELETE FROM campaigns WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM campaign_members") == 0
    assert _one(conn, "SELECT count(*) FROM campaign_transcripts") == 0
    assert _one(conn, "SELECT count(*) FROM transcripts") == 3  # transcripts outlive campaigns
    assert _one(conn, f"SELECT campaign_id FROM recordings WHERE id = '{R}'") is None
    assert _one(conn, f"SELECT campaign_id FROM jobs WHERE id = '{J}'") is None


def test_profile_delete_unbinds_recording_speakers(conn):
    conn.execute("DELETE FROM profiles WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM campaign_members") == 0
    assert _one(conn, f"SELECT profile_id FROM recording_speakers WHERE recording_id = '{R}'") is None


def test_recording_delete_cascades_to_every_child(conn):
    conn.execute(f"DELETE FROM recordings WHERE id = '{R}'")
    for table in ("recording_discord", "recording_speakers", "recording_segments",
                  "recording_markers", "recording_rejoins"):
        assert _one(conn, f"SELECT count(*) FROM {table}") == 0, table
    assert _one(conn, f"SELECT recording_id FROM jobs WHERE id = '{J}'") is None
    conn.execute(f"DELETE FROM recordings WHERE id = '{L}'")
    assert _one(conn, "SELECT count(*) FROM recording_devices") == 0


def test_every_fk_child_column_is_indexed(conn):
    """Unindexed FK children make every cascade a full scan. An index led by
    the FK's first column is enough: the subtype FKs' second column
    (``source``) is a constant pinned by CHECK."""
    missing = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM pragma_table_list WHERE schema = 'main' AND type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        fks: dict[int, list[str]] = {}
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            fks.setdefault(fk["id"], []).append(fk["from"])
        indexes = [
            [c["name"] for c in conn.execute(f"PRAGMA index_info('{ix['name']}')")]
            for ix in conn.execute(f"PRAGMA index_list({table})")
        ]
        pk = [r["name"] for r in sorted(conn.execute(f"PRAGMA table_info({table})"), key=lambda r: r["pk"])
              if r["pk"]]
        indexes.append(pk)
        for cols in fks.values():
            if not any(ix and ix[0] == cols[0] for ix in indexes):
                missing.append(f"{table}({', '.join(cols)})")
    assert missing == []
