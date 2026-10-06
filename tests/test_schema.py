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
        INSERT INTO campaigns (id, slug, display_name, folder, created_at)
          VALUES (1, 'game', 'Game', 'Game', 'now'), (2, 'other', 'Other', 'Other', 'now');
        INSERT INTO campaign_members (campaign_id, profile_id, discord_user_id) VALUES (1, 1, '111');
        INSERT INTO transcripts (id, stem, campaign_id, position, created_at)
          VALUES (1, 's1', 1, 0, 'now'), (2, 's2', 1, 1, 'now'), (3, 's3', NULL, NULL, 'now');
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
        INSERT INTO files (kind, root, rel_path, label, transcript_id, size, mtime_ns) VALUES
          ('transcript',   'output', 's1.md',                       NULL,          1, 10, 5),
          ('summary',      'output', 's1.summary.md',               NULL,          1, 10, 5),
          ('sidecar',      'output', 's1_diar.json',                NULL,          1, 10, 5),
          ('excerpt',      'output', 's1_excerpt_SPEAKER_00.mp3',   'SPEAKER_00',  1, 10, 5),
          ('excerpt_text', 'output', 's1_excerpt_SPEAKER_00.txt',   'SPEAKER_00',  1, 10, 5),
          ('audio',        'output', 's1.flac',                     NULL,          1, NULL, NULL),
          ('backup',       'output', 's1.md.bak',                   NULL,          1, 10, 5);
        INSERT INTO files (kind, root, rel_path, label, recording_id, size, mtime_ns) VALUES
          ('combined',   'data', 'recordings/{R}/combined.wav',        NULL,     '{R}', 10, 5),
          ('per_user',   'data', 'recordings/{R}/per-user/mic',        'mic',    '{R}', NULL, NULL),
          ('per_user',   'data', 'recordings/{R}/per-user/123456',     '123456', '{R}', NULL, NULL),
          ('live_draft', 'data', 'recordings/{R}/live_transcript.md',  NULL,     '{R}', 10, 5);
        INSERT INTO files (kind, root, rel_path, profile_id, size, mtime_ns)
          VALUES ('reference_clip', 'data', 'profiles/embeddings/alice.mp3', 1, 10, 5);
        INSERT INTO files (kind, root, rel_path, campaign_id, size, mtime_ns)
          VALUES ('journal', 'output', 'Game/Game Journal.md', 1, 10, 5);
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
    "strict type": "UPDATE transcripts SET position = 'first' WHERE id = 1",
    # campaigns
    "campaign slug unique": "INSERT INTO campaigns (slug, display_name, folder, created_at) "
                            "VALUES ('game', 'G', 'Fresh', 'now')",
    "campaign folder unique ignoring case": "INSERT INTO campaigns (slug, display_name, folder, created_at) "
                                            "VALUES ('x', 'X', 'GAME', 'now')",
    "campaign folder empty": "UPDATE campaigns SET folder = '' WHERE id = 1",
    "campaign folder with a slash": "UPDATE campaigns SET folder = 'a/b' WHERE id = 1",
    "campaign folder with a backslash": "UPDATE campaigns SET folder = 'a\\b' WHERE id = 1",
    "campaign folder with a colon": "UPDATE campaigns SET folder = 'a:b' WHERE id = 1",
    "campaign folder with a quote": "UPDATE campaigns SET folder = 'a\"b' WHERE id = 1",
    "campaign folder leading dot": "UPDATE campaigns SET folder = '.hidden' WHERE id = 1",
    "campaign folder trailing dot": "UPDATE campaigns SET folder = 'a.' WHERE id = 1",
    "campaign folder trailing space": "UPDATE campaigns SET folder = 'a ' WHERE id = 1",
    "campaign folder leading space": "UPDATE campaigns SET folder = ' a' WHERE id = 1",
    "campaign folder control character": "UPDATE campaigns SET folder = 'a' || char(7) || 'b' WHERE id = 1",
    "campaign folder embedded NUL": "UPDATE campaigns SET folder = 'a' || char(0) || '/b' WHERE id = 1",
    "campaign folder over 80 characters": "UPDATE campaigns SET folder = printf('%.81c', 'a') WHERE id = 1",
    "campaign folder over 200 bytes": "UPDATE campaigns SET folder = printf('%.80c', char(128512)) WHERE id = 1",
    "folder_pending equal to its folder": "UPDATE campaigns SET folder_pending = 'Game' WHERE id = 1",
    "folder_pending equal to another campaign's folder ignoring case":
        "UPDATE campaigns SET folder_pending = 'OTHER' WHERE id = 1",
    "folder_pending invalid like folder": "UPDATE campaigns SET folder_pending = 'a/b' WHERE id = 1",
    "folder_claimed boolean": "UPDATE campaigns SET folder_claimed = 2 WHERE id = 1",
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
    "root stem unique": "INSERT INTO transcripts (stem, created_at) VALUES ('s3', 'now')",
    "stem unique within a campaign":
        "INSERT INTO transcripts (stem, campaign_id, position, created_at) VALUES ('s1', 1, 9, 'now')",
    "stem empty": "INSERT INTO transcripts (stem, created_at) VALUES ('', 'now')",
    "stem slash": "INSERT INTO transcripts (stem, created_at) VALUES ('a/b', 'now')",
    "stem backslash": "INSERT INTO transcripts (stem, created_at) VALUES ('a\\b', 'now')",
    # a transcript's campaign and position
    "transcript in an unknown campaign": "UPDATE transcripts SET campaign_id = 99, position = 5 WHERE id = 3",
    "campaign without position": "UPDATE transcripts SET position = NULL WHERE id = 1",
    "position without campaign": "UPDATE transcripts SET position = 0 WHERE id = 3",
    "position unique within a campaign": "UPDATE transcripts SET campaign_id = 1, position = 0 WHERE id = 3",
    "position negative": "UPDATE transcripts SET campaign_id = 1, position = -1 WHERE id = 3",
    # journal_entries
    "journal entry outside its campaign": "INSERT INTO journal_entries VALUES (2, 2, 'now')",
    "journal entry unassigned transcript": "INSERT INTO journal_entries VALUES (3, 1, 'now')",
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
    # files
    "file with no owner": "INSERT INTO files (kind, root, rel_path) VALUES ('audio', 'output', 'x.flac')",
    "file with two owners": "INSERT INTO files (kind, root, rel_path, transcript_id, campaign_id) "
                            "VALUES ('audio', 'output', 'x.flac', 2, 1)",
    "transcript kind with a recording owner": f"INSERT INTO files (kind, root, rel_path, recording_id) "
                                              f"VALUES ('audio', 'output', 'x.flac', '{R}')",
    "recording kind with a transcript owner": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                              "VALUES ('combined', 'output', 'x.wav', 2)",
    "profile kind with a campaign owner": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                          "VALUES ('reference_clip', 'data', 'profiles/embeddings/x.mp3', 2)",
    "journal kind with a profile owner": "INSERT INTO files (kind, root, rel_path, profile_id) "
                                         "VALUES ('journal', 'output', 'Other/Other Journal.md', 2)",
    "transcript-owned file under the data root": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                                 "VALUES ('audio', 'data', 'x.flac', 2)",
    "campaign-owned file under the data root": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                               "VALUES ('journal', 'data', 'Other/Other Journal.md', 2)",
    "file kind enum": "INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('video', 'output', 'x.mp4', 2)",
    "file root enum": "INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('audio', 'cache', 'x.flac', 2)",
    "excerpt without label": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                             "VALUES ('excerpt', 'output', 's2_excerpt_A.mp3', 2)",
    "summary with a label": "INSERT INTO files (kind, root, rel_path, label, transcript_id) "
                            "VALUES ('summary', 'output', 's2.summary.md', 'A', 2)",
    "file label empty": "INSERT INTO files (kind, root, rel_path, label, transcript_id) "
                        "VALUES ('excerpt', 'output', 's2_excerpt_.mp3', '', 2)",
    "file label with separator": "INSERT INTO files (kind, root, rel_path, label, transcript_id) "
                                 "VALUES ('excerpt', 'output', 's2_excerpt_a_b.mp3', 'a/b', 2)",
    "per_user label not a track name": f"INSERT INTO files (kind, root, rel_path, label, recording_id) "
                                       f"VALUES ('per_user', 'data', 'recordings/{R}/per-user/abc', 'abc', '{R}')",
    "file path unique per root": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                 "VALUES ('audio', 'output', 's1.flac', 2)",
    "second audio for a transcript": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                     "VALUES ('audio', 'output', 's1b.flac', 1)",
    "second summary for a transcript": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                       "VALUES ('summary', 'output', 's1b.summary.md', 1)",
    "second sidecar for a transcript": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                       "VALUES ('sidecar', 'output', 's1b_diar.json', 1)",
    "duplicate excerpt label": "INSERT INTO files (kind, root, rel_path, label, transcript_id) "
                               "VALUES ('excerpt', 'output', 's1_excerpt_SPEAKER_00b.mp3', 'SPEAKER_00', 1)",
    "second combined for a recording": f"INSERT INTO files (kind, root, rel_path, recording_id) "
                                       f"VALUES ('combined', 'data', 'recordings/{R}/combined.wav', '{R}')",
    "second clip for a profile": "INSERT INTO files (kind, root, rel_path, profile_id) "
                                 "VALUES ('reference_clip', 'data', 'profiles/embeddings/x.mp3', 1)",
    "second journal for a campaign": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                     "VALUES ('journal', 'output', 'Game/Game 2 Journal.md', 1)",
    "journal outside a campaign folder": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                         "VALUES ('journal', 'output', 'Other Journal.md', 2)",
    "journal deeper than a campaign folder": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                             "VALUES ('journal', 'output', 'a/b/c Journal.md', 2)",
    "journal not named Journal.md": "INSERT INTO files (kind, root, rel_path, campaign_id) "
                                    "VALUES ('journal', 'output', 'Other/journal.md', 2)",
    "output file deeper than one folder": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                          "VALUES ('audio', 'output', 'a/b/x.flac', 2)",
    "file path with an embedded NUL": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                      "VALUES ('audio', 'output', 'a.flac' || char(0) || '/b/c.flac', 2)",
    "file path absolute": "INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('audio', 'output', '/x.flac', 2)",
    "file path dotdot": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                        "VALUES ('audio', 'output', 'a/../x.flac', 2)",
    "file path backslash": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                           "VALUES ('audio', 'output', 'a\\x.flac', 2)",
    "file path drive prefix": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                              "VALUES ('audio', 'output', 'C:x.flac', 2)",
    "file path trailing slash": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                "VALUES ('audio', 'output', 'dir/', 2)",
    "file path empty": "INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('audio', 'output', '', 2)",
    "combined path mismatch": f"INSERT INTO files (kind, root, rel_path, recording_id) "
                              f"VALUES ('combined', 'data', 'recordings/{L}/combined.wav', '{R}')",
    "per_user path mismatch": f"INSERT INTO files (kind, root, rel_path, label, recording_id) "
                              f"VALUES ('per_user', 'data', 'recordings/{R}/per-user/system', 'mic', '{R}')",
    "live_draft path mismatch": f"INSERT INTO files (kind, root, rel_path, recording_id) "
                                f"VALUES ('live_draft', 'data', 'recordings/{L}/live_transcript.md', '{R}')",
    "audio path ending .md": "INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('audio', 'output', 'x.md', 2)",
    "backup not ending .md.bak": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                 "VALUES ('backup', 'output', 'x.md', 2)",
    "transcript path not .md": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                               "VALUES ('transcript', 'output', 's2.txt', 2)",
    "transcript path is a summary": "INSERT INTO files (kind, root, rel_path, transcript_id) "
                                    "VALUES ('transcript', 'output', 's2.summary.md', 2)",
    "file size without mtime": "INSERT INTO files (kind, root, rel_path, transcript_id, size) "
                               "VALUES ('audio', 'output', 'x.flac', 2, 5)",
    "file size negative": "INSERT INTO files (kind, root, rel_path, transcript_id, size, mtime_ns) "
                          "VALUES ('audio', 'output', 'x.flac', 2, -1, 5)",
    "per_user directory has no size": f"UPDATE files SET size = 1, mtime_ns = 1 WHERE kind = 'per_user' AND recording_id = '{R}'",
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
    for table in ("journal_entries", "transcript_speakers",
                  "search_index_state", "search_blocks"):
        assert _one(conn, f"SELECT count(*) FROM {table} WHERE transcript_id = 1") == 0, table
    assert _one(conn, "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'strahd'") == 0
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None
    assert _one(conn, f"SELECT transcript_id FROM jobs WHERE id = '{J}'") is None  # history kept


def test_title_index_follows_transcript_insert_rename_delete(conn):
    def titles(term):
        return [r[0] for r in conn.execute(
            "SELECT rowid FROM transcript_titles WHERE transcript_titles MATCH ? ORDER BY rowid", (term,))]

    assert titles("s1") == [1]
    conn.execute("INSERT INTO transcripts (id, stem, created_at) VALUES (9, 'Kings and Queens', 'now')")
    assert titles("king") == [9]  # porter: "kings" -> "king"
    conn.execute("UPDATE transcripts SET stem = 'Remove Your Mask' WHERE id = 9")
    assert titles("king") == [] and titles("mask") == [9]
    conn.execute("DELETE FROM transcripts WHERE id IN (1, 9)")
    assert titles("s1") == [] and titles("mask") == []
    conn.execute("INSERT INTO transcript_titles (transcript_titles, rank) VALUES ('integrity-check', 1)")


def test_deleting_recordings_transcript_makes_it_transcribable_again(conn):
    conn.execute("DELETE FROM transcripts WHERE id = 3")
    assert _one(conn, f"SELECT transcript_id FROM recordings WHERE id = '{R}'") is None


def test_unassigning_journaled_transcript_drops_entry_and_marks_stale(conn):
    conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM journal_entries") == 0
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None


def test_campaign_delete_is_refused_while_it_holds_sessions(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM campaigns WHERE id = 1")


def test_campaign_delete(conn):
    conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE campaign_id = 1")
    conn.execute("DELETE FROM campaigns WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM campaign_members") == 0
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
    assert _one(conn, "SELECT count(*) FROM files WHERE recording_id IS NOT NULL") == 0
    conn.execute(f"DELETE FROM recordings WHERE id = '{L}'")
    assert _one(conn, "SELECT count(*) FROM recording_devices") == 0


def _file_count(conn, column, value):
    return _one(conn, f"SELECT count(*) FROM files WHERE {column} = ?", value)


def test_file_rows_cascade_with_their_owner(conn):
    conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE campaign_id = 1")
    for column, value, table in (("transcript_id", 1, "transcripts"),
                                 ("recording_id", R, "recordings"),
                                 ("profile_id", 1, "profiles"),
                                 ("campaign_id", 1, "campaigns")):
        assert _file_count(conn, column, value) > 0, column
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (value,))
        assert _file_count(conn, column, value) == 0, column


def test_profile_key_rename_rewrites_its_clip_row(conn):
    conn.execute("UPDATE profiles SET key = 'alicia' WHERE id = 1")
    assert _one(conn, "SELECT rel_path FROM files WHERE profile_id = 1") == "profiles/embeddings/alicia.mp3"
    assert _one(conn, "SELECT count(*) FROM files WHERE profile_id = 2") == 0


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


# ---------------------------------------------------------------------------
# Campaign assignment, folders, and their triggers
# ---------------------------------------------------------------------------

def test_stem_rename_marks_the_journal_stale(conn):
    conn.execute("UPDATE transcripts SET stem = 's1 renamed' WHERE id = 1")
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None
    assert _one(conn, "SELECT count(*) FROM journal_entries") == 1


def test_moving_a_journaled_session_drops_its_entry_and_marks_the_old_journal_stale(conn):
    conn.execute("UPDATE transcripts SET campaign_id = 2, position = 0 WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM journal_entries") == 0
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is not None
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 2") is None


def test_reordering_a_journaled_session_keeps_its_entry(conn):
    conn.execute("UPDATE transcripts SET position = 7 WHERE id = 1")
    assert _one(conn, "SELECT count(*) FROM journal_entries") == 1
    assert _one(conn, "SELECT journal_stale_since FROM campaigns WHERE id = 1") is None


def test_the_same_stem_may_live_in_two_campaigns_and_the_root(conn):
    conn.execute("INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                 "VALUES ('s3', 2, 0, 'now')")
    conn.execute("INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                 "VALUES ('s1', 2, 1, 'now')")
    assert _one(conn, "SELECT count(*) FROM transcripts WHERE stem = 's1'") == 2


def test_campaign_id_zero_and_the_root_may_share_a_stem(conn):
    conn.execute("INSERT INTO campaigns (id, slug, display_name, folder, created_at) "
                 "VALUES (0, 'zero', 'Zero', 'Zero', 'now')")
    conn.execute("INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                 "VALUES ('s3', 0, 0, 'now')")
    assert _one(conn, "SELECT count(*) FROM transcripts WHERE stem = 's3'") == 2


def test_a_deleted_transcript_id_is_never_reused(conn):
    top = _one(conn, "SELECT max(id) FROM transcripts")
    conn.execute("DELETE FROM transcripts WHERE id = ?", (top,))
    new = conn.execute("INSERT INTO transcripts (stem, created_at) VALUES ('fresh', 'now') RETURNING id"
                       ).fetchone()[0]
    assert new > top


def test_a_new_campaign_cannot_take_another_campaigns_pending_folder(conn):
    conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO campaigns (slug, display_name, folder, created_at) "
                     "VALUES ('x', 'X', 'RENAMED', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE campaigns SET folder = 'renamed' WHERE id = 2")


def test_a_case_only_folder_rename_can_be_pending(conn):
    conn.execute("UPDATE campaigns SET folder_pending = 'GAME' WHERE id = 1")
    assert _one(conn, "SELECT folder_pending FROM campaigns WHERE id = 1") == "GAME"


def test_the_folder_check_accepts_reserved_names(conn):
    """Reserved Windows names are refused by the sanitizer and the app, not by SQL."""
    conn.execute("UPDATE campaigns SET folder = 'CON' WHERE id = 1")


def test_a_folder_prefix_rewrite_handles_brackets(conn):
    conn.execute("UPDATE campaigns SET folder = 'a[b]' WHERE id = 1")
    conn.execute("UPDATE files SET rel_path = 'a[b]/' || substr(rel_path, length('Game/') + 1) "
                 "WHERE kind = 'journal' AND rel_path GLOB '?*/?* Journal.md'")
    assert _one(conn, "SELECT rel_path FROM files WHERE kind = 'journal'") == "a[b]/Game Journal.md"


def test_v11_ddl_backslashes(conn):
    """A lost backslash in the Python string would silently weaken the CHECKs."""
    for table, needles in (("transcripts", ("[/\\]",)), ("campaigns", ("[/\\:",))):
        sql = _one(conn, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", table)
        for needle in needles:
            assert needle in sql, (table, needle)


def test_db_py_has_no_invalid_escapes():
    """Python keeps an invalid escape such as ``\\]`` literally; only the warning shows it."""
    import warnings
    from pathlib import Path

    path = Path(db.__file__)
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_active_jobs_have_a_partial_index(conn):
    sql = _one(conn, "SELECT sql FROM sqlite_master WHERE name = 'jobs_active'")
    assert "status IN ('pending', 'running')" in sql
